from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx

from .config import settings
from .opportunity import KEYWORDS  # Mantido para compatibilidade; a consulta usa DJEN_TERMS.

log = logging.getLogger(__name__)

DJEN_URL = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"
DJEN_USER_AGENT = "ASPIN-Garimpo/1.0"
# Termos deliberadamente distintos. KEYWORDS continua sendo o helper de classificação.
# Medido em 16/10/2026 (100 itens por termo): % com valor >= R$ 100 mil citado.
# "precatório" (11%) e "RPV" (HTTP 500 "sistema muito ocupado") saíram por baixa
# precisão/instabilidade; "expedição de precatório" (34%) e "expeça-se precatório" entraram.
DJEN_TERMS = (
    "ofício requisitório",
    "expedição de precatório",
    "requisição de pagamento",
    "expedição de RPV",
    "expeça-se precatório",
    "ordem de pagamento",
)


def djen_queries() -> list[tuple[str, str | None]]:
    """Pares (termo, tribunal). DJEN_TRIBUNALS vazio = todos os tribunais do DJEN."""
    tribunals = [t.strip().upper() for t in str(_setting("djen_tribunals", "") or "").split(",") if t.strip()]
    return [(term, tribunal) for term in DJEN_TERMS for tribunal in (tribunals or [None])]

TJSP_URLS = {
    "tjsp_requisitorios": "https://esaj.tjsp.jus.br/esaj?servico=950000",
    "tjsp_precatorios": "https://www.tjsp.jus.br/Precatorios",
}


@dataclass
class DjenResult:
    """Resultado da coleta DJEN, incluindo cobertura e erros observados."""

    rows: list[dict]
    errors: list[str]
    totals: dict[str, int]
    pages: int = 0
    limited: bool = False
    cursor: dict | None = None
    retry_cursors: list[dict] = field(default_factory=list)


@dataclass
class _PageFetch:
    payload: dict[str, Any] | None = None
    global_stop: bool = False
    budget_exhausted: bool = False


def _setting(name: str, default: Any) -> Any:
    """Lê settings sem exigir que versões antigas de config tenham os novos campos."""

    return getattr(settings, name, default)


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(_setting(name, default))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def _nonnegative_float(name: str, default: float) -> float:
    try:
        value = float(_setting(name, default))
    except (TypeError, ValueError):
        value = default
    return max(0.0, value)


def _safe_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.casefold() in {"", "none", "null"}:
        return ""
    return text


def _row_identity(row: dict[str, Any]) -> str:
    """Obtém identidade estável sem nunca usar a chave literal ``None``."""

    for field in ("hash", "id", "numeroComunicacao"):
        value = _safe_value(row.get(field))
        if value:
            return value

    text = _safe_value(row.get("texto"))
    tribunal = _safe_value(row.get("siglaTribunal") or row.get("tribunal"))
    process = _safe_value(row.get("numeroProcesso") or row.get("numero_processo"))
    published = _safe_value(
        row.get("dataDisponibilizacao") or row.get("data_disponibilizacao")
    )
    fallback = "\x1f".join((text, tribunal, process, published))
    return hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def _extract_count(payload: dict[str, Any]) -> tuple[int | None, bool]:
    """Retorna (count, inválido); count ausente é permitido para compatibilidade."""

    raw = None
    found = False
    for field in ("count", "total", "totalCount"):
        if field in payload:
            raw = payload[field]
            found = True
            break
    if not found or raw is None:
        return None, False
    if isinstance(raw, bool):
        return None, True
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return None, True
    return (count, count < 0)


def _append_error(errors: list[str], message: str) -> None:
    if message not in errors:
        errors.append(message)


async def _fetch_page(
    client: httpx.AsyncClient,
    params: dict[str, Any],
    errors: list[str],
    request_count: list[int],
    max_requests: int,
    request_delay: float,
    timeout_retries: int = 3,
) -> _PageFetch:
    """Faz uma página com no máximo três tentativas para rede/429/5xx.

    Cada tentativa conta no teto global de requests. O atraso de retry cresce
    exponencialmente, mas nunca passa de 30 segundos.
    """

    retry_reason: str | None = None
    for attempt in range(timeout_retries):
        if request_count[0] >= max_requests:
            if retry_reason is not None:
                _append_error(
                    errors,
                    f"{retry_reason}: retries interrompidos pelo limite de requests",
                )
            return _PageFetch(budget_exhausted=True)
        if request_count[0] > 0:
            delay = min(30.0, request_delay * (2**attempt))
            if delay:
                await asyncio.sleep(delay)
        request_count[0] += 1
        try:
            response = await client.get(DJEN_URL, params=params)
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            retry_reason = f"djen_network: {type(exc).__name__}"
            if attempt + 1 < timeout_retries:
                continue
            _append_error(
                errors,
                f"djen_network: {type(exc).__name__} após {timeout_retries} tentativas",
            )
            return _PageFetch()

        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            _append_error(errors, "djen_shape: resposta sem status HTTP válido")
            return _PageFetch()

        if status == 403:
            # Não tentar contornar bloqueio geográfico, CAPTCHA ou qualquer
            # outro controle de acesso.
            _append_error(errors, "djen_http_403: acesso negado — a API do DJEN recusa IPs fora do Brasil (ex.: runners do GitHub); rode o DJEN em servidor no Brasil")
            return _PageFetch(global_stop=True)

        if status == 429 or 500 <= status <= 599:
            retry_reason = f"djen_http_{status}"
            if attempt + 1 < timeout_retries:
                continue
            _append_error(errors, f"djen_http_{status}: falha após {timeout_retries} tentativas")
            return _PageFetch()

        if status == 401:
            _append_error(errors, "djen_http_401: não autorizado")
            return _PageFetch()
        if status < 200 or status >= 300:
            _append_error(errors, f"djen_http_{status}: resposta rejeitada")
            return _PageFetch()

        try:
            payload = response.json()
        except (TypeError, ValueError):
            _append_error(errors, "djen_json: resposta não é JSON válido")
            return _PageFetch()
        if not isinstance(payload, dict):
            _append_error(errors, "djen_shape: resposta JSON não é objeto")
            return _PageFetch()
        if str(payload.get("status", "")).casefold() == "error" and not isinstance(payload.get("items"), list):
            # A API responde {"status":"error","message":"O sistema está muito ocupado"}.
            retry_reason = "djen_busy"
            if attempt + 1 < timeout_retries:
                continue
            _append_error(errors, f"djen_busy: API ocupada após {timeout_retries} tentativas")
            return _PageFetch()
        return _PageFetch(payload=payload)

    _append_error(errors, "djen_network: tentativas esgotadas")
    return _PageFetch()


def _date_range_newest_first(since: date, until: date):
    current = until
    while current >= since:
        yield current
        current -= timedelta(days=1)


def _cursor_value(cursor: dict, name: str, default: int) -> int | None:
    value = cursor.get(name, default)
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value


def _make_cursor(since: date, until: date, date_index: int, term_index: int, page: int) -> dict:
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "date_index": date_index,
        "term_index": term_index,
        "page": page,
    }


def _next_cursor(
    since: date,
    until: date,
    date_index: int,
    term_index: int,
    page: int,
    has_more: bool,
    date_count: int,
) -> dict | None:
    if has_more:
        return _make_cursor(since, until, date_index, term_index, page + 1)
    if term_index + 1 < len(djen_queries()):
        return _make_cursor(since, until, date_index, term_index + 1, 1)
    if date_index + 1 < date_count:
        return _make_cursor(since, until, date_index + 1, 0, 1)
    return None


async def search_djen(
    since: date,
    until: date,
    cursor: dict | None = None,
    stop_after_query: bool = False,
) -> DjenResult:
    """Consulta a API DJEN por termo e por dia, do dia mais novo para o antigo.

    Falhas isoladas ficam em retry_cursors sem bloquear outras consultas.
    Bloqueios globais e limites preservam a tentativa atual. stop_after_query
    restringe uma recuperação ao mesmo termo/dia, evitando repetir outras buscas.
    """

    result = DjenResult(rows=[], errors=[], totals={})
    if since > until:
        result.errors.append("djen_date_range: since posterior a until")
        return result

    dates = list(_date_range_newest_first(since, until))
    date_count = len(dates)
    date_index = 0
    term_index = 0
    page = 1
    if cursor is not None:
        if not isinstance(cursor, dict):
            result.errors.append("djen_cursor: formato inválido")
            result.limited = True
            result.cursor = _make_cursor(since, until, 0, 0, 1)
            return result
        if cursor.get("since") != since.isoformat() or cursor.get("until") != until.isoformat():
            result.errors.append("djen_cursor: janela não corresponde a since/until")
            result.limited = True
            result.cursor = _make_cursor(since, until, 0, 0, 1)
            return result
        date_index = _cursor_value(cursor, "date_index", 0)
        term_index = _cursor_value(cursor, "term_index", 0)
        page = _cursor_value(cursor, "page", 1)
        if (
            date_index is None
            or term_index is None
            or page is None
            or date_index < 0
            or date_index >= date_count
            or term_index < 0
            or term_index >= len(djen_queries())
            or page < 1
        ):
            # Lista de termos/tribunais mudou desde a última execução: recomeça a
            # janela do início. Não é falha da fonte.
            log.warning("Cursor DJEN incompatível com a configuração atual; recomeçando a janela")
            date_index, term_index, page = 0, 0, 1

    page_size = _positive_int("djen_page_size", 50)
    max_pages = _positive_int("djen_max_pages", 10)
    max_documents = _positive_int("djen_max_documents", 2500)
    max_requests = _positive_int("djen_max_requests", 80)
    request_delay = _nonnegative_float("djen_request_delay", 0.5)
    timeout_seconds = _nonnegative_float("djen_timeout_seconds", 30.0) or 30.0

    queries = djen_queries()
    unique_rows: dict[str, dict] = {}
    request_count = [0]
    stop_all = False
    page_counts: dict[str, int] = {}
    current_cursor: dict | None = _make_cursor(
        since, until, date_index, term_index, page
    )

    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        headers={"User-Agent": DJEN_USER_AGENT},
    ) as client:
        while date_index < date_count and not stop_all:
            day = dates[date_index]
            while term_index < len(queries) and not stop_all:
                term, tribunal = queries[term_index]
                key = f"{day.isoformat()}:{term}" + (f":{tribunal}" if tribunal else "")
                result.totals.setdefault(key, 0)
                page_in_cycle = page_counts.get(key, 0)
                # count é o total da consulta; ao retomar, as páginas anteriores
                # são representadas pelo deslocamento nominal page-1.
                seen_for_query = (page - 1) * page_size
                empty_retry_for_page = False

                while not stop_all:
                    current_cursor = _make_cursor(
                        since, until, date_index, term_index, page
                    )
                    if request_count[0] >= max_requests:
                        result.limited = True
                        stop_all = True
                        break

                    params = {
                        "texto": term,
                        "dataDisponibilizacaoInicio": day.isoformat(),
                        "dataDisponibilizacaoFim": day.isoformat(),
                        "itensPorPagina": page_size,
                        "pagina": page,
                        "meio": "D",
                    }
                    if tribunal:
                        params["siglaTribunal"] = tribunal
                    fetched = await _fetch_page(
                        client,
                        params,
                        result.errors,
                        request_count,
                        max_requests,
                        request_delay,
                    )
                    if fetched.budget_exhausted:
                        result.limited = True
                        stop_all = True
                        break
                    if fetched.global_stop:
                        result.limited = True
                        stop_all = True
                        break
                    payload = fetched.payload
                    if payload is None:
                        result.retry_cursors.append(current_cursor)
                        if stop_after_query:
                            result.limited = True
                            stop_all = True
                            break
                        current_cursor = _next_cursor(since, until, date_index, term_index, page, False, date_count)
                        term_index += 1
                        page = 1
                        break

                    items = payload.get("items")
                    if not isinstance(items, list):
                        _append_error(
                            result.errors,
                            f"djen_shape: items inválido em {key}:pagina={page}",
                        )
                        result.limited = True
                        stop_all = True
                        break
                    if any(not isinstance(item, dict) for item in items):
                        _append_error(
                            result.errors,
                            f"djen_shape: item inválido em {key}:pagina={page}",
                        )
                        result.limited = True
                        stop_all = True
                        break

                    count, invalid_count = _extract_count(payload)
                    if invalid_count:
                        _append_error(
                            result.errors,
                            f"djen_shape: count inválido em {key}:pagina={page}",
                        )
                        result.limited = True
                        stop_all = True
                        break
                    result.pages += 1
                    if count is not None:
                        result.totals[key] = max(result.totals[key], count)
                    else:
                        result.totals[key] = max(
                            result.totals[key], seen_for_query + len(items)
                        )

                    if count is not None and count > 0 and not items:
                        if not empty_retry_for_page:
                            # Repetição única da mesma página para inconsistência
                            # transitória count>0 + items vazio.
                            empty_retry_for_page = True
                            continue
                        _append_error(
                            result.errors,
                            f"djen_empty_page_count_gt_zero: {key}:pagina={page}:count={count}",
                        )
                        result.limited = True
                        stop_all = True
                        break
                    empty_retry_for_page = False

                    page_in_cycle += 1
                    page_counts[key] = page_in_cycle
                    seen_for_query += len(items)
                    # Uma página é a unidade de limite: todos os seus itens são
                    # preservados antes de decidir o teto de documentos.
                    page_rows: list[dict] = []
                    page_identities: set[str] = set()
                    for row in items:
                        identity = _row_identity(row)
                        if identity not in unique_rows and identity not in page_identities:
                            page_identities.add(identity)
                            page_rows.append(row)
                    for row in page_rows:
                        unique_rows[_row_identity(row)] = row

                    if count is not None:
                        has_more = seen_for_query < max(count, 0)
                    else:
                        has_more = len(items) >= page_size
                    next_cursor = _next_cursor(
                        since,
                        until,
                        date_index,
                        term_index,
                        page,
                        has_more,
                        date_count,
                    )

                    if stop_after_query and not has_more:
                        current_cursor = None
                        stop_all = True
                        break

                    # Limites de página e request são por ciclo, não globais à
                    # janela. A retomada começa na página seguinte sem saltar.
                    if page_in_cycle >= max_pages and has_more:
                        result.limited = True
                        current_cursor = next_cursor
                        stop_all = True
                        break
                    if len(unique_rows) >= max_documents and next_cursor is not None:
                        result.limited = True
                        current_cursor = next_cursor
                        stop_all = True
                        break
                    if request_count[0] >= max_requests and next_cursor is not None:
                        result.limited = True
                        current_cursor = next_cursor
                        stop_all = True
                        break
                    if next_cursor is None:
                        current_cursor = None
                        stop_all = True
                        break

                    # Página/termo concluído: avançar somente após processar tudo.
                    current_cursor = next_cursor
                    if has_more:
                        page += 1
                    else:
                        term_index += 1
                        page = 1
                    break

                if stop_all:
                    break
            if stop_all:
                break
            date_index += 1
            term_index = 0
            page = 1

    result.rows = list(unique_rows.values())
    if stop_all and current_cursor is not None:
        result.cursor = current_cursor
        result.limited = True
    else:
        result.cursor = None
        result.limited = bool(result.retry_cursors)
    return result


@dataclass
class DjenProcessResult:
    """Comunicações de um processo no DJEN. ok=False: consulta incompleta (não usar como 'nada novo')."""

    rows: list[dict]
    errors: list[str]
    ok: bool = False
    blocked: bool = False  # 403 ou teto de requests: parar a passada inteira


def djen_row_id(row: dict[str, Any]) -> str:
    """Identidade estável de uma comunicação (hash/id da API ou hash do conteúdo)."""

    return _row_identity(row)


async def search_djen_process(
    numero: str,
    since: date,
    until: date,
    client: httpx.AsyncClient | None = None,
    request_count: list[int] | None = None,
    max_pages: int = 3,
) -> DjenProcessResult:
    """Comunicações do DJEN de um processo (numeroProcesso), entre since e until.

    Mesmos cuidados da busca por termo: timeout, até 3 tentativas para rede/429/5xx/
    "sistema ocupado", espaçamento entre requests e teto global (request_count
    compartilhado entre processos de uma mesma passada).
    """

    result = DjenProcessResult(rows=[], errors=[])
    digits = "".join(ch for ch in str(numero or "") if ch.isdigit())
    if len(digits) != 20:
        result.errors.append("djen_processo: número CNJ inválido")
        return result
    request_count = request_count if request_count is not None else [0]
    max_requests = _positive_int("djen_max_requests", 80)
    request_delay = _nonnegative_float("djen_request_delay", 0.5)
    page_size = 100
    own_client = client is None
    if own_client:
        timeout_seconds = _nonnegative_float("djen_timeout_seconds", 30.0) or 30.0
        client = httpx.AsyncClient(timeout=timeout_seconds, headers={"User-Agent": DJEN_USER_AGENT})
    unique: dict[str, dict] = {}
    try:
        for page in range(1, max(1, max_pages) + 1):
            params = {
                "numeroProcesso": digits,
                "dataDisponibilizacaoInicio": since.isoformat(),
                "dataDisponibilizacaoFim": until.isoformat(),
                "itensPorPagina": page_size,
                "pagina": page,
            }
            fetched = await _fetch_page(client, params, result.errors, request_count, max_requests, request_delay)
            if fetched.global_stop or fetched.budget_exhausted:
                result.blocked = True
                return result
            payload = fetched.payload
            if payload is None:
                return result
            items = payload.get("items")
            if not isinstance(items, list):
                _append_error(result.errors, "djen_shape: items inválido na consulta por processo")
                return result
            for row in items:
                if isinstance(row, dict):
                    unique.setdefault(_row_identity(row), row)
            count, _ = _extract_count(payload)
            seen = (page - 1) * page_size + len(items)
            has_more = seen < count if count is not None else len(items) >= page_size
            if not has_more:
                break
    finally:
        if own_client:
            await client.aclose()
    result.rows = list(unique.values())
    result.ok = True
    return result


def _redacted_exception_name(exc: BaseException) -> str:
    """Evita vazar URL, query string ou conteúdo de resposta nos logs."""

    return type(exc).__name__


async def probe_tjsp_pages() -> list[dict]:
    """Busca páginas TJSP somente para monitoramento; falhas não derrubam o ciclo."""

    rows: list[dict] = []
    timeout_seconds = _nonnegative_float("djen_timeout_seconds", 30.0) or 30.0
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": DJEN_USER_AGENT},
        ) as client:
            for source, url in TJSP_URLS.items():
                try:
                    response = await client.get(url)
                    status = getattr(response, "status_code", None)
                    if isinstance(status, int) and status >= 400:
                        log.warning("TJSP %s indisponível (http_%s)", source, status)
                        continue
                    raise_for_status = getattr(response, "raise_for_status", None)
                    if callable(raise_for_status):
                        raise_for_status()
                    text = str(getattr(response, "text", ""))
                    rows.append(
                        {
                            "source": source,
                            "external_id": url,
                            "title": source,
                            "text_excerpt": text[:4000],
                            "document_url": url,
                            "raw_data": {
                                "status_code": status,
                                "content_length": len(text),
                            },
                        }
                    )
                except Exception as exc:
                    log.warning(
                        "TJSP %s indisponível (erro=%s)",
                        source,
                        _redacted_exception_name(exc),
                    )
    except Exception as exc:
        log.warning("TJSP indisponível (erro=%s)", _redacted_exception_name(exc))
    return rows


def persist_source(row: dict) -> bool:
    """Grava o documento. Retorna True se for NOVO (nunca visto antes)."""

    if not getattr(settings, "database_url", ""):
        return True
    import psycopg

    source = row.get("source") or "djen"
    external_id = _safe_value(
        row.get("external_id")
        or row.get("hash")
        or row.get("id")
        or row.get("numeroComunicacao")
        or row.get("link")
    ) or _row_identity(row)
    last_error = None
    for attempt in range(4):
        try:
            conn = psycopg.connect(getattr(settings, "database_url"), connect_timeout=15)
            break
        except psycopg.OperationalError as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2**attempt)
    else:
        raise last_error
    with conn:
        cur = conn.execute(
            """insert into public.source_documents(source,tribunal_alias,external_id,process_number,published_at,title,text_excerpt,document_url,raw_data)
            values(%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict(source,external_id) do update set text_excerpt=excluded.text_excerpt,raw_data=excluded.raw_data
            returning (xmax = 0)""",
            (
                source,
                row.get("siglaTribunal"),
                external_id,
                row.get("numero_processo") or row.get("numeroProcesso"),
                row.get("data_disponibilizacao") or row.get("dataDisponibilizacao"),
                row.get("tipoComunicacao") or row.get("title"),
                row.get("texto") or row.get("text_excerpt") or "",
                row.get("link") or row.get("document_url"),
                json.dumps(row),
            ),
        )
        return bool(cur.fetchone()[0])
