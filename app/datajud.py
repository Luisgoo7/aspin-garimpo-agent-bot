import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import settings

BASE_URL = "https://api-publica.datajud.cnj.jus.br"
# Tabela Processual Unificada (CNJ): movimento 12457 = "Expedição de precatório/rpv".
MOVEMENT_EXPEDICAO_PRECATORIO_RPV = 12457
SOURCE_FIELDS = ["numeroProcesso", "classe", "orgaoJulgador", "grau", "tribunal",
                 "dataHoraUltimaAtualizacao", "movimentos.codigo", "movimentos.nome", "movimentos.dataHora"]
# 429 é rejeição rápida da fila (retentar é barato). 504 custa ~60 s e indica saturação do
# gateway do CNJ: não retentar (ref.: rvsanches/skills-datajud-djen, operação em produção).
RETRY_STATUSES = {429, 500, 502, 503}


class DataJudError(RuntimeError):
    """Erro sanitizado (sem chave, URL completa ou corpo de resposta)."""


class DataJudClient:
    def __init__(self, api_key: str | None = None, max_retries: int = 2):
        self.api_key = api_key or settings.datajud_api_key
        self.max_retries = max_retries

    async def search(self, tribunal_alias: str, query: dict[str, Any], client: httpx.AsyncClient | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise DataJudError("DATAJUD_API_KEY não configurada")
        url = f"{BASE_URL}/{tribunal_alias}/_search"
        headers = {"Authorization": f"APIKey {self.api_key}", "Content-Type": "application/json"}
        own = client is None
        client = client or httpx.AsyncClient(timeout=120)
        try:
            for attempt in range(self.max_retries):
                try:
                    response = await client.post(url, headers=headers, json=query)
                except httpx.HTTPError as exc:
                    if attempt + 1 < self.max_retries:
                        await asyncio.sleep(min(30, 2 ** (attempt + 1)))
                        continue
                    raise DataJudError(f"rede: {type(exc).__name__}") from None
                status = response.status_code
                if status in RETRY_STATUSES:
                    if attempt + 1 < self.max_retries:
                        await asyncio.sleep(_retry_delay(response, attempt))
                        continue
                    raise DataJudError(f"HTTP {status} após {self.max_retries} tentativas")
                if status == 504:
                    raise DataJudError("HTTP 504: DataJud sobrecarregado (sem nova tentativa)")
                if status in (401, 403):
                    raise DataJudError(f"HTTP {status}: chave pública DataJud recusada (verifique se o CNJ trocou a chave)")
                if status == 404:
                    raise DataJudError("HTTP 404: endpoint api_publica_* inexistente")
                if status >= 400:
                    raise DataJudError(f"HTTP {status}")
                try:
                    return response.json()
                except ValueError:
                    raise DataJudError("resposta não é JSON") from None
            raise DataJudError("tentativas esgotadas")
        finally:
            if own:
                await client.aclose()


def _retry_delay(response, attempt: int) -> float:
    try:
        delay = float(response.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        delay = 2 ** (attempt + 1)
    return min(30.0, max(1.0, delay))


def expedition_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=settings.datajud_max_expedition_age_days)).isoformat()


def build_expedition_query(after: list | None, since: str, size: int) -> dict:
    """Processos indexados desde `since` com movimento de expedição de precatório/RPV.

    Paginação por search_after sobre (dataHoraUltimaAtualizacao, numeroProcesso):
    o DataJud indexa com atraso de dias, então o cursor segue a data de indexação,
    não a data do movimento.
    """
    query = {
        "size": size,
        "track_total_hits": True,
        "_source": SOURCE_FIELDS,
        "sort": [{"dataHoraUltimaAtualizacao": {"order": "asc"}}, {"numeroProcesso.keyword": {"order": "asc"}}],
        "query": {"bool": {"filter": [
            {"term": {"movimentos.codigo": MOVEMENT_EXPEDICAO_PRECATORIO_RPV}},
            {"range": {"dataHoraUltimaAtualizacao": {"gte": since}}},
            {"range": {"movimentos.dataHora": {"gte": expedition_cutoff()}}},
        ]}},
    }
    if after:
        query["search_after"] = after
    return query


def hit_to_row(alias: str, hit: dict, cutoff: str = "") -> dict | None:
    source = hit.get("_source") or {}
    number = str(source.get("numeroProcesso") or "").strip()
    if not number:
        return None
    movements = [m for m in source.get("movimentos") or []
                 if isinstance(m, dict) and m.get("codigo") == MOVEMENT_EXPEDICAO_PRECATORIO_RPV]
    dates = sorted(str(m.get("dataHora") or "") for m in movements if m.get("dataHora"))
    last = dates[-1] if dates else None
    if cutoff and (not last or last < cutoff[:19]):
        return None  # Processo atualizado agora, mas a expedição é antiga.
    classe = (source.get("classe") or {}).get("nome") or "classe não informada"
    orgao = (source.get("orgaoJulgador") or {}).get("nome") or "órgão não informado"
    text = (f"DataJud {alias.removeprefix('api_publica_').upper()}: Expedição de precatório/RPV "
            f"registrada em {last or 'data não informada'} ({len(movements)} movimento(s)). "
            f"Processo {number}. Classe: {classe}. Órgão: {orgao}.")
    return {
        "source": "datajud",
        "external_id": f"{alias}:{number}:{source.get('grau') or ''}",
        "siglaTribunal": alias,
        "numeroProcesso": number,
        "data_disponibilizacao": last[:10] if last else None,
        "title": "Expedição de precatório/RPV (DataJud)",
        "texto": text,
        "metadata": {"classe": classe, "orgao": orgao, "grau": source.get("grau"),
                     "movimentos_expedicao": dates[-5:], "atualizado": source.get("dataHoraUltimaAtualizacao")},
    }


async def collect_expeditions(alias: str, cursor: dict, client: DataJudClient | None = None) -> tuple[list[dict], dict, dict]:
    """Coleta até DATAJUD_MAX_PAGES páginas a partir do cursor do tribunal.

    Retorna (linhas, novo_cursor, métricas). Falhas levantam DataJudError sem
    devolver cursor, então a próxima execução repete a mesma página.
    """
    client = client or DataJudClient()
    since = cursor.get("since") or (datetime.now(timezone.utc) - timedelta(days=settings.datajud_initial_lookback_days)).isoformat()
    after = cursor.get("after")
    rows: list[dict] = []
    pages = 0
    total = 0
    exhausted = False
    old = 0
    cutoff = expedition_cutoff()
    async with httpx.AsyncClient(timeout=settings.datajud_timeout_seconds) as http:
        while pages < settings.datajud_max_pages:
            if pages:
                await asyncio.sleep(settings.datajud_request_delay)  # respeita o limite da API pública
            data = await client.search(alias, build_expedition_query(after, since, settings.datajud_page_size), http)
            hits_data = data.get("hits") or {}
            hits = hits_data.get("hits") or []
            total_raw = hits_data.get("total", 0)
            total = total_raw.get("value", 0) if isinstance(total_raw, dict) else int(total_raw or 0)
            pages += 1
            for hit in hits:
                row = hit_to_row(alias, hit, cutoff)
                if row:
                    rows.append(row)
                else:
                    old += 1
            if hits and hits[-1].get("sort"):
                after = hits[-1]["sort"]
            if len(hits) < settings.datajud_page_size:
                exhausted = True
                break
    return rows, {"since": since, "after": after}, {"pages": pages, "documents": len(rows), "old_skipped": old, "total": total, "exhausted": exhausted}
