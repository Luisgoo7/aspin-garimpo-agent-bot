import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from .config import settings
from .datajud import DataJudClient
from .llm import LLMClassifier
from .opportunity import KEYWORDS, keyword_score
from .telegram_bot import send_alert
from .sources import search_djen, probe_tjsp_pages, persist_source

log = logging.getLogger(__name__)


def build_query(since: datetime) -> dict:
    return {"size": settings.datajud_page_size, "track_total_hits": False, "_source": ["numeroProcesso", "movimentos", "classe", "assuntos", "partes", "dataHoraUltimaAtualizacao", "url"], "sort": [{"dataHoraUltimaAtualizacao": {"order": "desc", "unmapped_type": "date"}}], "query": {"bool": {"filter": [{"range": {"movimentos.dataHora": {"gte": since.isoformat()}}}], "should": [{"match_phrase": {"movimentos.nome": term}} for term in KEYWORDS], "minimum_should_match": 1}}}


def _connect():
    import psycopg
    return psycopg.connect(settings.database_url)


def _db_datetime(value):
    if not value:
        return None
    if isinstance(value, (datetime, timezone)):
        return value
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _score(value) -> float:
    try:
        score = float(value or 0)
    except (TypeError, ValueError):
        score = 0.0
    if score > 1:
        score /= 100
    return min(0.9999, max(0.0, score))


def _tribunal_id(cur, alias: str):
    cur.execute("insert into public.tribunals(alias,name) values(%s,%s) on conflict(alias) do nothing", (alias, alias))
    cur.execute("select id from public.tribunals where alias=%s", (alias,))
    return cur.fetchone()[0]


def process_already_found(alias: str, cnj: str) -> bool:
    """True se o processo ja gerou oportunidade (evita reclassificar e alertar de novo)."""
    if not settings.database_url or not cnj:
        return False
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""select 1 from public.opportunities o join public.processes p on p.id=o.process_id
            join public.tribunals t on t.id=p.tribunal_id where t.alias=%s and p.cnj_number=%s limit 1""", (alias, cnj))
        return cur.fetchone() is not None


def save_opportunity(source: dict, alias: str, result: dict, text: str):
    """Grava a oportunidade. Retorna o id (novo) ou None se ja existia / sem banco."""
    if not settings.database_url:
        return "sem-banco"
    with _connect() as conn, conn.cursor() as cur:
        tribunal_id = _tribunal_id(cur, alias)
        cnj = source.get("numeroProcesso") or source.get("numero") or "unknown"
        cur.execute("""insert into public.processes(tribunal_id,cnj_number,class_name,subject,parties,last_movement_at,source_url,raw_data)
            values(%s,%s,%s,%s,%s,%s,%s,%s) on conflict (tribunal_id,cnj_number) do update set raw_data=excluded.raw_data,updated_at=now() returning id""",
            (tribunal_id, cnj, source.get("classe", {}).get("nome") if isinstance(source.get("classe"), dict) else None, source.get("assuntos"), json.dumps(source.get("partes", [])), _db_datetime(source.get("dataHoraUltimaAtualizacao")), source.get("url"), json.dumps(source)))
        process_id = cur.fetchone()[0]
        rtype = result.get("requisition_type")
        cur.execute("select 1 from public.opportunities where process_id=%s and requisition_type is not distinct from %s limit 1", (process_id, rtype))
        if cur.fetchone():
            return None
        cur.execute("insert into public.evidence(process_id,evidence_type,text_excerpt,source_payload) values(%s,%s,%s,%s) returning id", (process_id, rtype or "candidate", result.get("evidence_quote") or text[:2000], json.dumps(source)))
        evidence_id = cur.fetchone()[0]
        cur.execute("""insert into public.opportunities(process_id,evidence_id,requisition_type,amount,beneficiary,debtor_entity,lawyer,issued_at,deadline_at,potential_score,classification)
            values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""", (process_id, evidence_id, rtype, result.get("amount"), result.get("beneficiary"), result.get("debtor_entity"), result.get("lawyer"), _db_datetime(result.get("issued_at")), _db_datetime(result.get("deadline_at")), _score(result.get("potential_score")), json.dumps(result)))
        return cur.fetchone()[0]


def mark_alerted(opp_id) -> None:
    if not settings.database_url or opp_id == "sem-banco":
        return
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("update public.opportunities set alerted_at=now() where id=%s", (opp_id,))


class Cycle:
    """Estado de um ciclo de coleta: orcamento de IA, contadores e erros."""

    def __init__(self):
        self.classifier = LLMClassifier()
        self.budget = settings.max_classify_per_cycle
        self.classified = 0
        self.candidates = 0
        self.found = 0
        self.alerts = 0
        self.errors: list[str] = []

    async def handle(self, source: dict, alias: str, text: str) -> None:
        cnj = source.get("numeroProcesso") or ""
        if cnj and process_already_found(alias, cnj):
            return
        if self.budget <= 0:
            if "limite_ia" not in self.errors:
                self.errors.append("limite_ia")
                log.warning("Limite de %s classificacoes por ciclo atingido; sobra fica para o proximo", settings.max_classify_per_cycle)
            return
        self.budget -= 1
        self.candidates += 1
        result = await self.classifier.classify(text[:12000])
        self.classified += 1
        if result.get("error"):
            self.errors.append("ia: " + str(result.get("reason"))[:200])
            return
        if not result.get("is_opportunity"):
            return
        try:
            opp_id = save_opportunity(source, alias, result, text)
        except Exception as exc:
            log.exception("Falha ao salvar oportunidade")
            self.errors.append(f"persistencia: {exc!r}"[:200])
            return
        if opp_id is None:
            return
        self.found += 1
        score = _score(result.get("potential_score"))
        if settings.telegram_bot_token and settings.telegram_chat_id and score >= settings.min_confidence:
            try:
                score_pct = f"{score:.0%}"
                await send_alert(f"🚨 ASPIN | Nova oportunidade\n\n🏛️ Tribunal: {alias}\n📌 Tipo: {result.get('requisition_type') or 'não informado'}\n💰 Valor: {result.get('amount') or 'não informado'}\n👤 Beneficiário: {result.get('beneficiary') or 'não informado'}\n🏢 Devedor: {result.get('debtor_entity') or 'não informado'}\n📄 Processo: {source.get('numeroProcesso') or source.get('url') or '-'}\n🎯 Confiança: {score_pct}\n\n🧠 Motivo\n{result.get('reason') or 'não informado'}\n\n🔎 Evidência\n“{result.get('evidence_quote') or 'não informada'}”")
                mark_alerted(opp_id)
                self.alerts += 1
            except Exception as exc:
                log.exception("Falha ao enviar alerta Telegram")
                self.errors.append(f"telegram: {exc!r}"[:200])


def _djen_source(row: dict) -> tuple[dict, str, str]:
    alias = str(row.get("siglaTribunal") or "djen").lower()
    cnj = row.get("numero_processo") or row.get("numeroProcesso") or ""
    ext = str(row.get("hash") or row.get("id") or row.get("numeroComunicacao") or "")
    source = {"numeroProcesso": cnj or f"djen-{ext}", "url": row.get("link"), "origem": "djen", "tipoComunicacao": row.get("tipoComunicacao"), "dataDisponibilizacao": row.get("data_disponibilizacao") or row.get("dataDisponibilizacao")}
    text = str(row.get("texto") or "")
    return source, alias, text


def start_run() -> str | None:
    if not settings.database_url:
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("insert into public.collection_runs(mode,sources) values(%s,%s) returning id", ("scheduled", json.dumps(["djen", "datajud", "tjsp_probe"])))
            return cur.fetchone()[0]
    except Exception:
        log.exception("Nao foi possivel registrar collection_runs")
        return None


def finish_run(run_id, tribunals: int, cycle: Cycle, since: datetime, lookback: int) -> None:
    if not settings.database_url:
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            if run_id:
                cur.execute("update public.collection_runs set finished_at=now(), tribunals_count=%s, candidates_count=%s, errors=%s where id=%s", (tribunals, cycle.candidates, json.dumps(cycle.errors), run_id))
            cur.execute("insert into public.audit_log(action,metadata) values(%s,%s)", ("collection_completed", json.dumps({"since": since.isoformat(), "lookback_minutes": lookback, "tribunals": tribunals, "candidates": cycle.candidates, "classified": cycle.classified, "opportunities": cycle.found, "alerts": cycle.alerts, "errors": len(cycle.errors)})))
    except Exception:
        log.exception("Nao foi possivel finalizar o registro do ciclo")


async def poll_once(lookback_minutes: int | None = None) -> int:
    client, cycle = DataJudClient(), Cycle()
    run_id = start_run()
    lookback = lookback_minutes or settings.initial_lookback_days * 24 * 60
    since = datetime.now(timezone.utc) - timedelta(minutes=lookback)
    query = build_query(since)

    # 1) DJEN: publicacoes novas -> classificacao (descobre oficios sem saber o processo)
    djen_new = 0
    try:
        djen_rows = await search_djen(since.date(), datetime.now(timezone.utc).date())
        for row in djen_rows:
            try:
                if not persist_source(row):
                    continue
                djen_new += 1
                source, alias, text = _djen_source(row)
                if text and keyword_score(text) > 0.25:
                    await cycle.handle(source, alias, text)
            except Exception as exc:
                log.exception("Falha ao processar publicacao DJEN")
                cycle.errors.append(f"djen_item: {exc!r}"[:200])
        log.info("DJEN: %s publicacoes lidas, %s novas", len(djen_rows), djen_new)
    except Exception as exc:
        log.exception("Falha na consulta DJEN")
        cycle.errors.append(f"djen: {exc!r}"[:200])

    # 2) Paginas TJSP: apenas monitoramento (nao geram candidatos)
    try:
        for row in await probe_tjsp_pages():
            persist_source(row)
    except Exception as exc:
        log.exception("Falha na consulta dos portais TJSP")
        cycle.errors.append(f"tjsp: {exc!r}"[:200])

    # 3) DataJud: movimentos de ofício/RPV/precatório por tribunal
    semaphore = asyncio.Semaphore(1)

    async def poll_tribunal(alias: str) -> None:
        try:
            async with semaphore:
                data = await client.search(alias, query)
                for hit in data.get("hits", {}).get("hits", []):
                    source, text = hit.get("_source", {}), str(hit.get("_source", {}))
                    if keyword_score(text) <= 0.25:
                        continue
                    await cycle.handle(source, alias, text)
        except Exception as exc:
            log.exception("Falha no tribunal %s", alias)
            cycle.errors.append(f"datajud {alias}: {exc!r}"[:200])

    aliases = [x.strip() for x in settings.datajud_tribunals.split(',') if x.strip()]
    await asyncio.gather(*(poll_tribunal(alias) for alias in aliases))
    log.info("Ciclo concluido: candidatos=%s classificados=%s oportunidades=%s alertas=%s erros=%s", cycle.candidates, cycle.classified, cycle.found, cycle.alerts, len(cycle.errors))
    finish_run(run_id, len(aliases), cycle, since, lookback)
    return cycle.found


async def run_forever() -> None:
    first = True
    while True:
        try:
            await poll_once(None if first else settings.incremental_lookback_minutes)
        except Exception:
            log.exception("Ciclo falhou")
        first = False
        await asyncio.sleep(settings.poll_interval_minutes * 60)

if __name__ == "__main__":
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run_forever())
