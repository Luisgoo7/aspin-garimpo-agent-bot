import hashlib
import os
import uuid
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import settings
from .dbcheck import diagnose, format_diagnosis, repair_pooler_dsn
from .opportunity import canonical_alias, canonical_cnj, clean_text, keyword_score, max_amount_in_text

LOCK_ID = 418739261
APP_NAME = "aspin-garimpo-" + ("actions" if os.environ.get("GITHUB_ACTIONS") else "worker")
MIGRATIONS = ("001_initial_schema.sql", "002_sources_and_runs.sql", "003_reliable_processing.sql", "004_prefilter_and_signals.sql")


class DatabaseUnavailable(RuntimeError):
    """Falha de conexão com causa sanitizada (sem DSN, usuário ou senha)."""


def safe_error(exc: Exception) -> str:
    """Não inclui URL, corpo HTTP, DSN ou credenciais na mensagem operacional."""
    if isinstance(exc, DatabaseUnavailable):
        return str(exc)
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return type(exc).__name__ + (f" HTTP {code}" if code else "")


_DSN_CACHE: dict[str, str] = {}
DSN_REPAIRED = [False]


def effective_dsn() -> str:
    """DATABASE_URL, com a região do pooler corrigida quando o host original não existe.

    A correção é sinalizada em todo diagnóstico/resumo para o secret ser ajustado.
    """
    dsn = settings.database_url
    if dsn not in _DSN_CACHE:
        repaired = repair_pooler_dsn(dsn)
        _DSN_CACHE[dsn] = repaired or dsn
        DSN_REPAIRED[0] = bool(repaired)
    return _DSN_CACHE[dsn]


def connect():
    dsn = effective_dsn()
    for attempt in range(3):
        try:
            return psycopg.connect(dsn, connect_timeout=15, row_factory=dict_row, application_name=APP_NAME)
        except psycopg.OperationalError as exc:
            if attempt == 2:
                raise DatabaseUnavailable(format_diagnosis(diagnose(dsn, exc))) from None
            time.sleep(2 ** attempt)


def check_connection() -> str:
    """Testa a conexão e devolve o diagnóstico sanitizado do alvo."""
    with connect() as conn:
        version = conn.execute("show server_version").fetchone()["server_version"]
        holder = lock_holder(conn)
        runs = conn.execute("""select mode,outcome,started_at,finished_at from public.collection_runs
            order by started_at desc limit 3""").fetchall() if _table_exists(conn, "collection_runs") else []
    text = format_diagnosis(diagnose(effective_dsn())) + f" | conexão=ok, postgres={version.split()[0]}"
    if holder:
        text += f" | advisory lock antigo (não usado mais) preso em: {holder}"
    if runs:
        text += " | últimos ciclos: " + "; ".join(
            f"{r['started_at']:%Y-%m-%d %H:%M}Z {r['mode']} {r['outcome']}{'' if r['finished_at'] else ' (sem finished_at)'}" for r in runs)
    if DSN_REPAIRED[0]:
        text += (" | AVISO: a região do pooler no secret está sem hífen (ex.: ca-central1); "
                 "o coletor corrigiu em memória, mas corrija o secret")
    return text


def ensure_schema():
    root = Path(__file__).resolve().parent.parent
    with connect() as conn:
        for name in MIGRATIONS:
            conn.execute((root / name).read_text(encoding="utf-8"))


def _table_exists(conn, name: str) -> bool:
    return conn.execute("select to_regclass(%s) is not null as ok", ("public." + name,)).fetchone()["ok"]


def lock_holder(conn) -> str | None:
    """Quem segura o lock do coletor (sem IP nem texto de query)."""
    try:
        row = conn.execute("""select a.pid, a.application_name, a.state, a.backend_type,
                extract(epoch from now()-a.backend_start)::int as conectado_s,
                extract(epoch from now()-coalesce(a.state_change,a.backend_start))::int as parado_s
            from pg_locks l left join pg_stat_activity a on a.pid=l.pid
            where l.locktype='advisory' and l.objid=%s and l.granted and l.pid<>pg_backend_pid() limit 1""",
            (LOCK_ID,)).fetchone()
    except psycopg.Error:
        return "não foi possível consultar pg_locks"
    if not row:
        return None
    return (f"pid={row['pid']} app={row['application_name'] or '(vazio)'} estado={row['state']} "
            f"conectado_ha={row['conectado_s']}s parado_ha={row['parado_s']}s")


@contextmanager
def collector_lock():
    """Lease com prazo numa tabela, em vez de advisory lock de sessão.

    Advisory lock de sessão vaza atrás do pooler (Supavisor mantém a conexão do
    servidor viva após um processo morrer), travando todos os ciclos seguintes.
    O lease expira sozinho depois de CYCLE_TIMEOUT_SECONDS + 5 min.
    """
    holder = f"{APP_NAME}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    ttl = settings.cycle_timeout_seconds + 300
    with connect() as conn:
        row = conn.execute("""insert into public.collector_lease(name,holder,acquired_at,expires_at)
            values('garimpo',%s,now(),now()+make_interval(secs=>%s))
            on conflict(name) do update set holder=excluded.holder,acquired_at=excluded.acquired_at,expires_at=excluded.expires_at
            where collector_lease.expires_at<now() returning holder""", (holder, ttl)).fetchone()
    locked = bool(row and row["holder"] == holder)
    try:
        yield locked
    finally:
        if locked:
            with connect() as conn:
                conn.execute("update public.collector_lease set expires_at=now() where name='garimpo' and holder=%s", (holder,))


def lease_holder() -> str | None:
    with connect() as conn:
        row = conn.execute("""select holder, extract(epoch from expires_at-now())::int as expira_em_s
            from public.collector_lease where name='garimpo' and expires_at>now()""").fetchone()
    return f"{row['holder'].split(':')[0]} (expira em {row['expira_em_s']}s)" if row else None


def source_key(row: dict) -> str:
    explicit = row.get("external_id") or row.get("hash") or row.get("id") or row.get("numeroComunicacao")
    if explicit:
        return str(explicit)
    parts = [row.get("siglaTribunal"), row.get("numero_processo") or row.get("numeroProcesso"), row.get("data_disponibilizacao"), row.get("texto") or row.get("text_excerpt")]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def upsert_document(row: dict) -> str:
    source = row.get("source") or "djen"
    text = clean_text(row.get("texto") or row.get("text_excerpt") or "")
    digest = hashlib.sha256(text.encode()).hexdigest()
    alias = canonical_alias(row.get("siglaTribunal") or row.get("tribunal_alias") or "djen")
    cnj = canonical_cnj(row.get("numero_processo") or row.get("numeroProcesso") or "")
    amount = max_amount_in_text(text)
    status = initial_status(source, text, amount)
    with connect() as conn:
        record = conn.execute("""insert into public.source_documents
            (source,tribunal_alias,external_id,process_number,published_at,title,text_excerpt,document_url,raw_data,content_hash,processing_status,prefilter_amount)
            values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict(source,external_id) do update set
            text_excerpt=excluded.text_excerpt,document_url=excluded.document_url,raw_data=excluded.raw_data,
            content_hash=excluded.content_hash,updated_at=now(),prefilter_amount=excluded.prefilter_amount,
            processing_status=case when source_documents.content_hash is distinct from excluded.content_hash
                then excluded.processing_status else source_documents.processing_status end,
            processing_attempts=case when source_documents.content_hash is distinct from excluded.content_hash
                then 0 else source_documents.processing_attempts end,
            next_attempt_at=case when source_documents.content_hash is distinct from excluded.content_hash
                then now() else source_documents.next_attempt_at end
            returning id""", (source, alias, source_key(row), cnj or None,
            row.get("data_disponibilizacao") or row.get("dataDisponibilizacao"),
            row.get("tipoComunicacao") or row.get("title"), text,
            row.get("link") or row.get("document_url"), Jsonb(row), digest, status, amount)).fetchone()
        return record["id"]


def initial_status(source: str, text: str, amount: float | None) -> str:
    """Pré-filtro local: só vai para a IA o que tem sinal e valor citado acima do mínimo.

    Sem valor citado o alerta seria impossível (o alerta exige valor), então o
    documento fica guardado como needs_enrichment sem gastar IA. Sinais do
    DataJud são metadados de movimento, sem texto/valor: ficam como signal.
    """
    if source == "tjsp_probe":
        return "ignored"
    if source == "datajud":
        return "signal"
    if keyword_score(text) <= 0.25:
        return "ignored"
    if amount is None:
        return "needs_enrichment"
    if amount < settings.min_opportunity_amount:
        return "below_minimum"
    return "pending"


def claim_document() -> dict | None:
    with connect() as conn:
        return conn.execute("""with candidate as (
            select id from public.source_documents where
            ((processing_status in ('pending','retry') and next_attempt_at<=now()) or
             (processing_status='processing' and processing_started_at<now()-interval '30 minutes'))
            and processing_attempts<%s
            and (published_at is null or published_at>=now()-make_interval(days=>%s))
            order by prefilter_amount desc nulls last,published_at desc nulls last,first_seen_at
            for update skip locked limit 1)
            update public.source_documents d set processing_status='processing',processing_started_at=now(),
            processing_attempts=processing_attempts+1 from candidate where d.id=candidate.id returning d.*""",
            (settings.max_processing_attempts, settings.max_document_age_days)).fetchone()


def finish_document(doc: dict, status: str, result: dict | None = None, error: str | None = None):
    attempts = doc["processing_attempts"]
    if status == "retry" and attempts >= settings.max_processing_attempts:
        status = "failed"
    next_try = datetime.now(timezone.utc) + timedelta(minutes=min(360, 5 * 2 ** max(0, attempts - 1)))
    with connect() as conn:
        conn.execute("""update public.source_documents set processing_status=%s,processed_at=now(),
            last_error=%s,classification=%s,next_attempt_at=%s where id=%s""",
            (status, error, Jsonb(result) if result is not None else None, next_try, doc["id"]))


def queue_counts() -> dict:
    with connect() as conn:
        rows = conn.execute("select processing_status,count(*) as n from public.source_documents group by processing_status").fetchall()
        return {r["processing_status"]: r["n"] for r in rows}


def save_opportunity(doc: dict, result: dict) -> tuple[str, bool]:
    alias = canonical_alias(doc["tribunal_alias"])
    cnj = canonical_cnj(doc["process_number"] or "") or "documento-" + str(doc["id"])
    rtype = result["requisition_type"]
    with connect() as conn:
        old = conn.execute("""select o.* from public.opportunities o join public.processes p on p.id=o.process_id
            join public.tribunals t on t.id=p.tribunal_id where t.alias=any(%s)
            and (p.cnj_number=any(%s) or regexp_replace(p.cnj_number,'[^0-9]','','g')=%s)
            and o.requisition_type is not distinct from %s limit 1""",
            ([alias, alias.removeprefix("api_publica_")], [cnj, str(doc["process_number"] or "")], cnj, rtype)).fetchone()
        if old:
            if old["alerted_at"] is None and (old["amount"] is None or float(old["amount"]) < settings.min_opportunity_amount or result["potential_score"] >= float(old["potential_score"] or 0)):
                evidence = conn.execute("""insert into public.evidence(process_id,evidence_type,text_excerpt,document_url,sha256,source_payload)
                    values(%s,%s,%s,%s,%s,%s) returning id""", (old["process_id"], rtype, result["evidence_quote"], doc["document_url"], doc["content_hash"], Jsonb(doc.get("raw_data") or {}))).fetchone()["id"]
                conn.execute("""update public.opportunities set evidence_id=%s,amount=%s,potential_score=%s,classification=%s,
                    beneficiary=%s,debtor_entity=%s,lawyer=%s,status=%s where id=%s""", (evidence, result["amount"], result["potential_score"], Jsonb(result), result.get("beneficiary"), result.get("debtor_entity"), result.get("lawyer"), "new" if result["potential_score"] >= settings.min_confidence else "review", old["id"]))
            return old["id"], False
        conn.execute("insert into public.tribunals(alias,name) values(%s,%s) on conflict(alias) do nothing", (alias, alias.upper()))
        tribunal = conn.execute("select id from public.tribunals where alias=%s", (alias,)).fetchone()["id"]
        source = doc.get("raw_data") or {}
        process = conn.execute("""insert into public.processes(tribunal_id,cnj_number,source_url,raw_data)
            values(%s,%s,%s,%s) on conflict(tribunal_id,cnj_number) do update
            set source_url=coalesce(excluded.source_url,processes.source_url),raw_data=excluded.raw_data,updated_at=now()
            returning id""", (tribunal, cnj, doc["document_url"], Jsonb(source))).fetchone()["id"]
        evidence = conn.execute("""insert into public.evidence(process_id,evidence_type,text_excerpt,document_url,sha256,source_payload)
            values(%s,%s,%s,%s,%s,%s) returning id""",
            (process, rtype, result["evidence_quote"], doc["document_url"], doc["content_hash"], Jsonb(source))).fetchone()["id"]
        opp = conn.execute("""insert into public.opportunities(process_id,evidence_id,status,requisition_type,amount,
            beneficiary,debtor_entity,lawyer,potential_score,classification)
            values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (process, evidence, "new" if result["potential_score"] >= settings.min_confidence else "review", rtype,
            result["amount"], result.get("beneficiary"), result.get("debtor_entity"), result.get("lawyer"),
            result["potential_score"], Jsonb(result))).fetchone()["id"]
        return opp, True


def pending_alerts(limit: int = 120) -> list[dict]:
    with connect() as conn:
        return conn.execute("""select o.*,p.cnj_number,p.source_url,t.alias,e.text_excerpt,e.document_url
            from public.opportunities o join public.processes p on p.id=o.process_id
            join public.tribunals t on t.id=p.tribunal_id left join public.evidence e on e.id=o.evidence_id
            where o.alerted_at is null and o.potential_score>=%s and o.amount>=%s
            and o.status='new' order by o.created_at limit %s""",
            (settings.min_confidence, settings.min_opportunity_amount, limit)).fetchall()


def mark_alerted(opp_id):
    # Já enviado ao Telegram: insiste na marcação para não reenviar no próximo ciclo.
    for attempt in range(3):
        try:
            with connect() as conn:
                conn.execute("update public.opportunities set alerted_at=now() where id=%s and alerted_at is null", (opp_id,))
            return
        except (psycopg.OperationalError, DatabaseUnavailable):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def datajud_signal(cnj: str | None) -> dict | None:
    """Movimento 'Expedição de precatório/RPV' visto no DataJud para o processo."""
    cnj = canonical_cnj(cnj or "")
    if not cnj:
        return None
    with connect() as conn:
        return conn.execute("""select tribunal_alias,published_at from public.source_documents
            where source='datajud' and process_number=%s order by published_at desc nulls last limit 1""", (cnj,)).fetchone()


def get_state(key: str) -> dict:
    with connect() as conn:
        row = conn.execute("select value from public.bot_state where key=%s", (key,)).fetchone()
        return row["value"] if row else {}


def set_state(key: str, value: dict):
    with connect() as conn:
        conn.execute("""insert into public.bot_state(key,value) values(%s,%s)
            on conflict(key) do update set value=excluded.value,updated_at=now()""", (key, Jsonb(value)))


def start_run(sources: list[str] | None = None, mode: str = "scheduled") -> str:
    with connect() as conn:
        return conn.execute("insert into public.collection_runs(mode,sources) values(%s,%s) returning id",
            (mode, Jsonb(sources or ["djen", "datajud"]))).fetchone()["id"]


def finish_run(run_id, outcome: str, metrics: dict, errors: list[str]):
    with connect() as conn:
        conn.execute("""update public.collection_runs set finished_at=now(),outcome=%s,
            tribunals_count=%s,candidates_count=%s,errors=%s,metrics=%s where id=%s""",
            (outcome, metrics.get("tribunals", 0), metrics.get("classified", 0), Jsonb(errors), Jsonb(metrics), run_id))
        conn.execute("insert into public.audit_log(action,metadata) values(%s,%s)",
            ("collection_completed", Jsonb({"outcome": outcome, **metrics, "errors": errors})))
