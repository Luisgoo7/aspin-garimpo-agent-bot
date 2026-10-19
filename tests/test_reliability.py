"""Regressões do ciclo ponta a ponta: diagnóstico do banco, estados e fontes."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest

from app import datajud, dbcheck, sources, storage, worker
from app.config import settings

SECRET = "SenhaMuitoSecreta123"
DIRECT = f"postgresql://postgres:{SECRET}@db.abcdefghijklmnopqrst.supabase.co:5432/postgres"
POOLER = f"postgresql://postgres.abcdefghijklmnopqrst:{SECRET}@aws-0-sa-east-1.pooler.supabase.com:5432/postgres"


# ---------- diagnóstico sanitizado do banco ----------

def test_direct_ipv6_only_host_is_diagnosed_without_secrets(monkeypatch):
    monkeypatch.setattr(dbcheck, "resolve", lambda host, port: {"dns": "ok", "ipv4": 0, "ipv6": 1})
    exc = psycopg.OperationalError(f'connection to server at "db.abcdefghijklmnopqrst.supabase.co" (2600::1), port 5432 failed: Network is unreachable')
    text = dbcheck.format_diagnosis(dbcheck.diagnose(DIRECT, exc))
    assert "causa=ipv6_sem_rota" in text and "Session pooler" in text
    assert SECRET not in text and "abcdefghijklmnopqrst" not in text


def test_paused_project_dns_failure(monkeypatch):
    monkeypatch.setattr(dbcheck, "resolve", lambda host, port: {"dns": "nao_resolve", "ipv4": 0, "ipv6": 0})
    exc = psycopg.OperationalError('could not translate host name "db.abcdefghijklmnopqrst.supabase.co" to address')
    diag = dbcheck.diagnose(DIRECT, exc)
    assert diag["categoria"] == "dns_nao_resolve" and "pausado" in diag["dica"]


def test_rest_url_in_database_url_is_flagged():
    diag = dbcheck.diagnose("https://abcdefghijklmnopqrst.supabase.co/rest/v1/")
    assert diag["categoria"] == "url_rest_em_vez_de_postgres"


def test_pooler_requires_user_with_project_ref(monkeypatch):
    monkeypatch.setattr(dbcheck, "resolve", lambda host, port: {"dns": "ok", "ipv4": 2, "ipv6": 0})
    bad = POOLER.replace("postgres.abcdefghijklmnopqrst:", "postgres:")
    assert dbcheck.diagnose(bad)["categoria"] == "pooler_usuario_sem_ref"
    good = dbcheck.diagnose(POOLER)
    assert good["alvo"]["usuario_com_ref"] is True and "categoria" not in good


@pytest.mark.parametrize("message,category", [
    ("FATAL: password authentication failed for user", "senha_recusada"),
    ("FATAL: Tenant or user not found", "pooler_usuario_ou_projeto"),
    ("timeout expired", "timeout"),
    ("algo novo", "desconhecido"),
])
def test_driver_messages_are_categorized(message, category):
    assert dbcheck.classify_error(Exception(message))[0] == category


def test_connect_raises_sanitized_database_unavailable(monkeypatch):
    def boom(*args, **kwargs):
        raise psycopg.OperationalError(f"password authentication failed for user postgres {SECRET}")
    monkeypatch.setattr(storage.psycopg, "connect", boom)
    monkeypatch.setattr(storage.time, "sleep", lambda s: None)
    monkeypatch.setattr(dbcheck, "resolve", lambda host, port: {"dns": "ok", "ipv4": 1, "ipv6": 0})
    monkeypatch.setattr(settings, "database_url", POOLER)
    with pytest.raises(storage.DatabaseUnavailable) as info:
        storage.connect()
    assert "senha_recusada" in str(info.value)
    assert SECRET not in str(info.value) and SECRET not in storage.safe_error(info.value)


def test_main_returns_database_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@h/db")
    monkeypatch.setattr(settings, "telegram_bot_token", "t")
    monkeypatch.setattr(settings, "telegram_chat_id", "c")
    monkeypatch.setattr(settings, "groq_api_key", "g")
    monkeypatch.setattr(settings, "datajud_api_key", "d")
    monkeypatch.setattr("sys.argv", ["worker", "--once", "--simulate-failure", "db"])
    try:
        assert worker.main() == worker.EXIT_DATABASE
    finally:
        settings.simulate_failure = ""


# ---------- "busca falhou" ≠ "zero oportunidades" ----------

def base_metrics(**extra):
    metrics = {"djen_pages": 3, "tribunals": 2, "datajud_ok": 2, "datajud_failed": [],
               "ai_ok": 0, "ai_failed": 0, "alerts": 0, "alerts_failed": 0, "opportunities": 0, "limited": False}
    metrics.update(extra)
    return metrics


def test_zero_opportunities_after_complete_search_is_success():
    assert worker.decide_outcome(base_metrics(), []) == "success"
    assert worker.OUTCOME_EXIT["success"] == 0


def test_djen_without_any_page_is_source_failure():
    outcome = worker.decide_outcome(base_metrics(djen_pages=0), ["djen_http_500: falha após 3 tentativas"])
    assert outcome == "source_failed" and worker.OUTCOME_EXIT[outcome] != 0


def test_all_datajud_tribunals_failing_is_source_failure():
    metrics = base_metrics(datajud_ok=0, datajud_failed=["api_publica_tjsp", "api_publica_trf3"])
    assert worker.decide_outcome(metrics, ["datajud api_publica_tjsp: HTTP 503"]) == "source_failed"


def test_isolated_failure_is_partial_not_success():
    metrics = base_metrics(datajud_ok=1, datajud_failed=["api_publica_trf3"])
    assert worker.decide_outcome(metrics, ["datajud api_publica_trf3: HTTP 503"]) == "partial"


def test_ai_and_telegram_total_failures_are_errors():
    assert worker.decide_outcome(base_metrics(ai_failed=3), ["ia: groq HTTP 429"]) == "ai_failed"
    assert worker.decide_outcome(base_metrics(ai_ok=1, alerts_failed=1), ["telegram: x"]) == "alert_failed"
    assert worker.decide_outcome(base_metrics(ai_ok=2, ai_failed=1), ["ia: x"]) == "partial"


def test_status_message_marks_failure_outcome():
    msg = worker.status_message({"outcome": "source_failed", "queue": {}}, ["djen_simulado: falha simulada"])
    assert "FALHA (source_failed)" in msg and "não é confirmação de ausência" in msg


@pytest.mark.asyncio
async def test_simulated_telegram_failure_keeps_alert_pending(monkeypatch):
    marked = []
    monkeypatch.setattr(storage, "pending_alerts", lambda limit: [{"id": "opp", "amount": 200000, "potential_score": 0.9}])
    monkeypatch.setattr(storage, "mark_alerted", marked.append)
    monkeypatch.setattr(settings, "simulate_failure", "telegram")
    metrics = {"alerts": 0}
    errors = []
    await worker._send_pending_alerts(metrics, errors)
    assert not marked and metrics["alerts_failed"] == 1 and errors == ["telegram: Telegram: falha simulada"]


# ---------- pré-filtro local ----------

@pytest.mark.parametrize("source,text,expected", [
    ("djen", "Expeça-se ofício requisitório no valor de R$ 250.000,00", "pending"),
    ("djen", "Expeça-se ofício requisitório no valor de R$ 5.000,00", "below_minimum"),
    ("djen", "Expeça-se ofício requisitório.", "needs_enrichment"),
    ("djen", "Intime-se a parte autora.", "ignored"),
    ("datajud", "DataJud TJSP: Expedição de precatório/RPV registrada", "signal"),
])
def test_prefilter_status(source, text, expected):
    from app.opportunity import max_amount_in_text
    assert storage.initial_status(source, text, max_amount_in_text(text)) == expected


# ---------- DataJud: paginação search_after, 429 e expedição antiga ----------

def hit(number, when, sort):
    return {"_source": {"numeroProcesso": number, "grau": "G1", "classe": {"nome": "Precatório"},
                        "movimentos": [{"codigo": 12457, "nome": "Expedição de precatório/rpv", "dataHora": when},
                                       {"codigo": 11010, "nome": "Mero expediente", "dataHora": when}]},
            "sort": sort}


class FakeDataJud:
    def __init__(self, pages):
        self.pages = list(pages)
        self.queries = []

    async def search(self, alias, query, client=None):
        self.queries.append(query)
        return self.pages.pop(0)


@pytest.mark.asyncio
async def test_datajud_paginates_with_search_after(monkeypatch):
    monkeypatch.setattr(settings, "datajud_page_size", 2)
    monkeypatch.setattr(settings, "datajud_max_pages", 5)
    recent = "2099-01-01T10:00:00.000Z"
    fake = FakeDataJud([
        {"hits": {"total": {"value": 3}, "hits": [hit("1", recent, [1, "1"]), hit("2", recent, [2, "2"])]}},
        {"hits": {"total": {"value": 3}, "hits": [hit("3", recent, [3, "3"])]}},
    ])
    rows, cursor, info = await datajud.collect_expeditions("api_publica_tjsp", {}, fake)
    assert [r["numeroProcesso"] for r in rows] == ["1", "2", "3"]
    assert "search_after" not in fake.queries[0] and fake.queries[1]["search_after"] == [2, "2"]
    assert cursor["after"] == [3, "3"] and info["exhausted"] and info["pages"] == 2
    assert fake.queries[0]["query"]["bool"]["filter"][0] == {"term": {"movimentos.codigo": 12457}}


@pytest.mark.asyncio
async def test_datajud_page_limit_keeps_backlog_cursor(monkeypatch):
    monkeypatch.setattr(settings, "datajud_page_size", 1)
    monkeypatch.setattr(settings, "datajud_max_pages", 1)
    fake = FakeDataJud([{"hits": {"total": {"value": 9}, "hits": [hit("1", "2099-01-01T00:00:00Z", [1, "1"])]}}])
    rows, cursor, info = await datajud.collect_expeditions("api_publica_tjsp", {"since": "2026-01-01T00:00:00+00:00"}, fake)
    assert not info["exhausted"] and cursor == {"since": "2026-01-01T00:00:00+00:00", "after": [1, "1"]}


def test_datajud_skips_old_expedition_of_recently_updated_process():
    assert datajud.hit_to_row("api_publica_tjsp", hit("1", "2019-09-05T15:07:50.000Z", [1, "1"]), "2026-01-01T00:00:00") is None
    row = datajud.hit_to_row("api_publica_tjsp", hit("1", "2026-09-05T15:07:50.000Z", [1, "1"]), "2026-01-01T00:00:00")
    assert row["source"] == "datajud" and row["data_disponibilizacao"] == "2026-09-05"


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_datajud_429_backs_off_then_succeeds(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(datajud.asyncio, "sleep", fake_sleep)
    http = FakeHttp([FakeResponse(429, headers={"retry-after": "7"}), FakeResponse(200, {"hits": {"hits": []}})])
    data = await datajud.DataJudClient("k").search("api_publica_tjsp", {}, http)
    assert data == {"hits": {"hits": []}} and sleeps == [7.0] and http.calls == 2


@pytest.mark.asyncio
async def test_datajud_429_does_not_loop_forever(monkeypatch):
    monkeypatch.setattr(datajud.asyncio, "sleep", AsyncMock())
    http = FakeHttp([FakeResponse(429)] * 3)
    with pytest.raises(datajud.DataJudError, match="HTTP 429 após 3 tentativas"):
        await datajud.DataJudClient("k").search("api_publica_tjsp", {}, http)
    assert http.calls == 3


@pytest.mark.asyncio
async def test_datajud_failure_does_not_advance_cursor(monkeypatch):
    saved = {}
    monkeypatch.setattr(settings, "datajud_tribunals", "api_publica_tjsp,api_publica_trf3")
    monkeypatch.setattr(settings, "enable_datajud", True)
    monkeypatch.setattr(storage, "get_state", lambda key: {})
    monkeypatch.setattr(storage, "set_state", lambda key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(storage, "upsert_document", lambda row: "id")

    async def collect(alias, cursor):
        if alias == "api_publica_trf3":
            raise datajud.DataJudError("HTTP 503 após 3 tentativas")
        return [{"source": "datajud"}], {"since": "s", "after": [1, "x"]}, {"documents": 1, "pages": 1, "exhausted": True}
    monkeypatch.setattr(worker, "collect_expeditions", collect)
    metrics = {"tribunals": 0, "datajud_documents": 0, "datajud_pages": 0, "datajud_ok": 0, "datajud_failed": []}
    errors = []
    await worker._collect_datajud(metrics, errors)
    assert list(saved) == ["datajud_cursor:api_publica_tjsp"]
    assert metrics["datajud_failed"] == ["api_publica_trf3"] and errors == ["datajud api_publica_trf3: HTTP 503 após 3 tentativas"]


# ---------- DJEN ----------

def test_djen_terms_exclude_unstable_broad_terms():
    assert "RPV" not in sources.DJEN_TERMS and "precatório" not in sources.DJEN_TERMS


def test_djen_tribunal_filter_expands_queries(monkeypatch):
    monkeypatch.setattr(settings, "djen_tribunals", "tjsp, trf3")
    queries = sources.djen_queries()
    assert len(queries) == 2 * len(sources.DJEN_TERMS) and queries[0] == (sources.DJEN_TERMS[0], "TJSP")


@pytest.mark.asyncio
async def test_djen_busy_payload_is_retried_not_treated_as_empty(monkeypatch):
    monkeypatch.setattr(sources.asyncio, "sleep", AsyncMock())

    class Client:
        calls = 0

        async def get(self, url, params=None):
            Client.calls += 1
            if Client.calls == 1:
                return FakeResponse(200, {"status": "error", "message": "O sistema está muito ocupado"})
            return FakeResponse(200, {"count": 0, "items": []})
    errors = []
    fetched = await sources._fetch_page(Client(), {}, errors, [0], 10, 0)
    assert fetched.payload == {"count": 0, "items": []} and not errors and Client.calls == 2


@pytest.mark.asyncio
async def test_simulated_djen_failure_is_recorded_as_error_not_zero(monkeypatch):
    """Ciclo com a fonte derrubada: registra source_failed no banco e não 'zero oportunidades'."""
    recorded = {}
    monkeypatch.setattr(settings, "simulate_failure", "djen")
    monkeypatch.setattr(settings, "enable_datajud", False)
    monkeypatch.setattr(settings, "enable_tjsp_probe", False)
    for key in ("database_url", "telegram_bot_token", "telegram_chat_id", "groq_api_key"):
        monkeypatch.setattr(settings, key, "x")

    class Lock:
        def __enter__(self):
            return True

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(storage, "collector_lock", lambda: Lock())
    monkeypatch.setattr(storage, "ensure_schema", lambda: None)
    monkeypatch.setattr(storage, "get_state", lambda key: {})
    monkeypatch.setattr(storage, "set_state", lambda key, value: recorded.setdefault("state", {}).__setitem__(key, value))
    monkeypatch.setattr(storage, "start_run", lambda sources, mode: recorded.setdefault("mode", mode) and "run")
    monkeypatch.setattr(storage, "finish_run", lambda run_id, outcome, metrics, errors: recorded.update(outcome=outcome, errors=list(errors)))
    monkeypatch.setattr(storage, "claim_document", lambda: None)
    monkeypatch.setattr(storage, "pending_alerts", lambda limit: [])
    monkeypatch.setattr(storage, "queue_counts", lambda: {})
    monkeypatch.setattr(worker, "send_alert", AsyncMock())
    result = await worker.run_cycle(60)
    assert result["outcome"] == "source_failed" and recorded["outcome"] == "source_failed"
    assert recorded["mode"] == "simulation"
    assert any("falha simulada" in e for e in recorded["errors"])
    assert "last_collection" not in recorded.get("state", {})  # janela não é dada como coberta
    assert worker.OUTCOME_EXIT[result["outcome"]] == worker.EXIT_SOURCE_FAILED


def test_password_with_unencoded_at_is_flagged(monkeypatch):
    monkeypatch.setattr(dbcheck, "resolve", lambda host, port: {"dns": "nao_resolve", "ipv4": 0, "ipv6": 0})
    diag = dbcheck.diagnose("postgresql://postgres:abc@def@db.abcdefghijklmnopqrst.supabase.co:5432/postgres")
    assert diag["categoria"] == "senha_sem_url_encoding"


def test_placeholder_and_unknown_host_are_reported_without_host(monkeypatch):
    monkeypatch.setattr(dbcheck, "resolve", lambda host, port: {"dns": "nao_resolve", "ipv4": 0, "ipv6": 0})
    assert dbcheck.diagnose("postgresql://postgres:[YOUR-PASSWORD]@db.abcdefghijklmnopqrst.supabase.co:5432/postgres")["categoria"] == "placeholder_na_uri"
    text = dbcheck.format_diagnosis(dbcheck.diagnose(f"postgresql://u:{SECRET}@meu-servidor-secreto.exemplo.com:5432/postgres"))
    assert "host=(oculto).exemplo.com" in text and "meu-servidor-secreto" not in text and SECRET not in text


def test_database_url_whitespace_is_removed():
    from app.config import Settings
    config = Settings(_env_file=None, database_url=" postgresql://u:p@aws-0-sa-east-1.pooler.\n supabase.com:5432/postgres \n")
    assert config.database_url == "postgresql://u:p@aws-0-sa-east-1.pooler.supabase.com:5432/postgres"


def test_pooler_region_missing_hyphen_is_repaired(monkeypatch):
    ok = {"aws-1-ca-central-1.pooler.supabase.com"}
    monkeypatch.setattr(dbcheck, "resolve", lambda host, port: {"dns": "ok" if host in ok else "nao_resolve", "ipv4": 1, "ipv6": 0})
    broken = f"postgresql://postgres.abcdefghijklmnopqrst:{SECRET}@aws-1-ca-central1.pooler.supabase.com:5432/postgres"
    assert dbcheck.repair_pooler_dsn(broken) == broken.replace("ca-central1", "ca-central-1")
    assert dbcheck.repair_pooler_dsn(broken.replace("ca-central1", "ca-central-1")) is None
    assert dbcheck.repair_pooler_dsn(DIRECT) is None
