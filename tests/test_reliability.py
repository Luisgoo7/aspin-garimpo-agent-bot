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
    http = FakeHttp([FakeResponse(429)] * 2)
    with pytest.raises(datajud.DataJudError, match="HTTP 429 após 2 tentativas"):
        await datajud.DataJudClient("k").search("api_publica_tjsp", {}, http)
    assert http.calls == 2


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
            raise datajud.DataJudError("HTTP 503 após 2 tentativas")
        return [{"source": "datajud"}], {"since": "s", "after": [1, "x"]}, {"documents": 1, "pages": 1, "exhausted": True}
    monkeypatch.setattr(worker, "collect_expeditions", collect)
    metrics = {"tribunals": 0, "datajud_documents": 0, "datajud_pages": 0, "datajud_ok": 0, "datajud_failed": []}
    errors = []
    await worker._collect_datajud(metrics, errors)
    assert [k for k in saved if k.startswith("datajud_cursor")] == ["datajud_cursor:api_publica_tjsp"]
    assert metrics["datajud_failed"] == ["api_publica_trf3"] and errors == ["datajud api_publica_trf3: HTTP 503 após 2 tentativas"]


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
    monkeypatch.setattr(worker, "digest_slot_due", lambda now: None)
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


@pytest.mark.asyncio
async def test_long_429_switches_provider_and_pauses_it(monkeypatch):
    from app import llm
    llm._COOLDOWN_UNTIL.clear()
    monkeypatch.setattr(settings, "groq_api_key", "g")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "nvidia_api_key", "n")
    sleeps = []
    monkeypatch.setattr(llm.asyncio, "sleep", AsyncMock(side_effect=lambda s: sleeps.append(s)))
    calls = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append(url)
            return FakeResponse(429, {}, {"retry-after": "60"})
    monkeypatch.setattr(llm.httpx, "AsyncClient", Client)
    result = await llm.LLMClassifier().classify("RPV expedida R$ 200.000,00")
    assert result["error"] and sleeps == []  # não espera 60s
    assert len(calls) == 2  # uma tentativa por provedor
    again = await llm.LLMClassifier().classify("RPV expedida R$ 200.000,00")
    assert len(calls) == 2 and "em pausa" in again["reason"]
    llm._COOLDOWN_UNTIL.clear()


def test_djen_can_be_disabled_without_counting_as_failure():
    metrics = base_metrics(djen_pages=0, djen_disabled=True)
    assert worker.decide_outcome(metrics, []) == "success"



# ---------- entrega: só as 3 melhores às 08/12/18h ----------

from datetime import datetime, timezone


@pytest.mark.parametrize("utc,expected", [
    ("2026-10-19T11:05:00", "08:00"),   # 08:05 em Brasília
    ("2026-10-19T13:30:00", "08:00"),   # 10:30, ainda dentro da tolerância de 3h
    ("2026-10-19T14:30:00", None),      # 11:30, 08h expirou e 12h não chegou
    ("2026-10-19T15:00:00", "12:00"),
    ("2026-10-19T21:59:00", "18:00"),
    ("2026-10-20T02:00:00", None),      # 23h
    ("2026-10-19T10:59:00", None),      # 07:59
])
def test_digest_slots_in_brasilia_time(utc, expected):
    slot = worker.digest_slot_due(datetime.fromisoformat(utc).replace(tzinfo=timezone.utc))
    assert (slot.strftime("%H:%M") if slot else None) == expected


class DigestStore:
    def __init__(self, opps):
        self.opps = opps
        self.state = {}
        self.marked = []
        self.archived = None

    def install(self, monkeypatch):
        monkeypatch.setattr(storage, "get_state", lambda key: self.state.get(key, {}))
        monkeypatch.setattr(storage, "set_state", lambda key, value: self.state.__setitem__(key, value))
        monkeypatch.setattr(storage, "pending_alerts", lambda limit: [o for o in self.ranked() if o["id"] not in self.marked][:limit])
        monkeypatch.setattr(storage, "mark_alerted", self.marked.append)
        monkeypatch.setattr(storage, "datajud_signal", lambda cnj: None)
        monkeypatch.setattr(storage, "archive_unselected", lambda ids, cutoff: setattr(self, "archived", list(ids)) or len(self.opps) - len(ids))

    def ranked(self):
        return sorted(self.opps, key=lambda o: o["amount"] * o["potential_score"], reverse=True)


def opp(i, amount, score):
    return {"id": f"o{i}", "amount": amount, "potential_score": score, "classification": {}, "alias": "api_publica_trf3"}


SLOT_TIME = datetime(2026, 10, 19, 15, 5, tzinfo=timezone.utc)  # 12:05 em Brasília


@pytest.mark.asyncio
async def test_digest_sends_only_top_three_once(monkeypatch):
    store = DigestStore([opp(i, 100000 * (i + 1), 0.8 + i * 0.01) for i in range(10)])
    store.install(monkeypatch)
    sender = AsyncMock()
    monkeypatch.setattr(worker, "send_alert", sender)
    metrics = {"alerts": 0}
    await worker._deliver_digest(metrics, [], SLOT_TIME)
    assert sender.await_count == 3 and store.marked == ["o9", "o8", "o7"]
    assert metrics["digest"] == "entregue" and store.archived == ["o9", "o8", "o7"]
    await worker._deliver_digest(metrics, [], SLOT_TIME.replace(minute=50))
    assert sender.await_count == 3 and metrics["digest"] == "ja_entregue"


@pytest.mark.asyncio
async def test_digest_waits_outside_delivery_hours(monkeypatch):
    store = DigestStore([opp(1, 500000, 0.9)])
    store.install(monkeypatch)
    sender = AsyncMock()
    monkeypatch.setattr(worker, "send_alert", sender)
    metrics = {"alerts": 0}
    await worker._deliver_digest(metrics, [], datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc))  # 17h: 12h já expirou, 18h não chegou
    assert sender.await_count == 0 and metrics["digest"] == "fora_do_horario"


@pytest.mark.asyncio
async def test_digest_telegram_failure_retries_next_hour_without_duplicates(monkeypatch):
    store = DigestStore([opp(i, 200000, 0.9) for i in range(5)])
    store.install(monkeypatch)
    calls = []

    async def flaky(text):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("Telegram HTTP 502: envio não confirmado")
    monkeypatch.setattr(worker, "send_alert", flaky)
    metrics = {"alerts": 0}
    errors = []
    await worker._deliver_digest(metrics, errors, SLOT_TIME)
    assert metrics["digest"] == "envio_pendente" and len(store.marked) == 1 and store.archived is None
    metrics2 = {"alerts": 0}
    await worker._deliver_digest(metrics2, [], SLOT_TIME.replace(hour=16))  # próximo ciclo, 13h
    assert metrics2["digest"] == "entregue" and len(store.marked) == 3 and len(set(store.marked)) == 3


@pytest.mark.asyncio
async def test_digest_without_candidates_sends_single_notice(monkeypatch):
    store = DigestStore([])
    store.install(monkeypatch)
    sender = AsyncMock()
    monkeypatch.setattr(worker, "send_alert", sender)
    metrics = {"alerts": 0}
    await worker._deliver_digest(metrics, [], SLOT_TIME)
    assert sender.await_count == 1 and "Nenhuma oportunidade" in sender.await_args.args[0]


def test_transient_failure_is_partial_until_it_repeats(monkeypatch):
    state = {}
    monkeypatch.setattr(storage, "get_state", lambda key: state.get(key, {}))
    monkeypatch.setattr(storage, "set_state", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setattr(settings, "simulate_failure", "")
    outcomes = [worker.apply_failure_streak("ai_failed", {}) for _ in range(3)]
    assert outcomes == ["partial", "partial", "ai_failed"]
    assert worker.apply_failure_streak("success", {}) == "success" and state["failure_streak"] == {}
    assert worker.apply_failure_streak("ai_failed", {}) == "partial"


def test_simulation_is_always_reported_as_failure(monkeypatch):
    monkeypatch.setattr(storage, "get_state", lambda key: {})
    monkeypatch.setattr(storage, "set_state", lambda key, value: None)
    monkeypatch.setattr(settings, "simulate_failure", "djen")
    assert worker.apply_failure_streak("source_failed", {}) == "source_failed"


@pytest.mark.asyncio
async def test_status_goes_to_channel_only_on_failure(monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(worker, "send_alert", sender)
    monkeypatch.setattr(storage, "get_state", lambda key: {})
    monkeypatch.setattr(storage, "set_state", lambda key, value: None)
    await worker.maybe_send_status({"outcome": "success", "queue": {}}, [])
    await worker.maybe_send_status({"outcome": "partial", "queue": {}}, ["x"])
    assert sender.await_count == 0
    await worker.maybe_send_status({"outcome": "source_failed", "queue": {}}, ["djen_http_403"])
    assert sender.await_count == 1



@pytest.mark.asyncio
async def test_old_queue_items_without_amount_skip_ai_and_do_not_count(monkeypatch):
    docs = iter([{"id": f"d{i}", "processing_attempts": 1, "source": "djen", "raw_data": {},
                  "text_excerpt": "Expeça-se ofício requisitório."} for i in range(30)]
                + [{"id": "rico", "processing_attempts": 1, "source": "djen", "raw_data": {},
                    "text_excerpt": "Expeça-se ofício requisitório de R$ 300.000,00"}, None])
    finished = []
    monkeypatch.setattr(storage, "claim_document", lambda: next(docs))
    monkeypatch.setattr(storage, "finish_document", lambda d, status, *a, **k: finished.append((d["id"], status)))
    monkeypatch.setattr(storage, "save_opportunity", lambda d, r: ("o", True))
    monkeypatch.setattr(settings, "max_classify_per_cycle", 1)
    classify = AsyncMock(return_value={"is_opportunity": True, "amount": 300000, "potential_score": 0.9,
                                       "requisition_type": "oficio_requisitorio", "evidence_quote": "R$ 300.000,00"})
    monkeypatch.setattr(worker.LLMClassifier, "classify", classify)
    metrics = {"classified": 0, "opportunities": 0, "below_minimum": 0}
    await worker._process_queue(metrics, [])
    assert classify.await_count == 1 and metrics["prefiltered"] == 30
    assert ("rico", "classified") in finished


@pytest.mark.asyncio
async def test_datajud_time_budget_rotates_tribunals(monkeypatch):
    state = {}
    monkeypatch.setattr(settings, "datajud_tribunals", "api_publica_tjsp,api_publica_trf3,api_publica_trf1")
    monkeypatch.setattr(settings, "enable_datajud", True)
    monkeypatch.setattr(settings, "datajud_time_budget_seconds", 30)
    monkeypatch.setattr(storage, "get_state", lambda key: state.get(key, {}))
    monkeypatch.setattr(storage, "set_state", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setattr(storage, "upsert_document", lambda row: "id")
    clock = SimpleNamespace(t=0.0)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock.t))
    seen = []

    async def collect(alias, cursor):
        seen.append(alias)
        clock.t += 100  # cada tribunal estoura o orçamento de 30s
        return [], {"since": "s"}, {"documents": 0, "pages": 1, "exhausted": True}
    monkeypatch.setattr(worker, "collect_expeditions", collect)
    metrics = {"tribunals": 0, "datajud_documents": 0, "datajud_pages": 0, "datajud_ok": 0, "datajud_failed": []}
    await worker._collect_datajud(metrics, [])
    assert seen == ["api_publica_tjsp"] and metrics["datajud_not_reached"] == ["api_publica_trf3", "api_publica_trf1"]
    assert worker.decide_outcome(dict(metrics, djen_pages=1, ai_ok=0, ai_failed=0, alerts=0, alerts_failed=0), []) == "partial"
    seen.clear()
    await worker._collect_datajud({"tribunals": 0, "datajud_documents": 0, "datajud_pages": 0, "datajud_ok": 0, "datajud_failed": []}, [])
    assert seen == ["api_publica_trf3"]


# ---------- modelo do alerta (cabeçalho PJe / ofício requisitório) ----------

def oficio_opp(**extra):
    classification = {"amount_basis": "principal", "process_class": "CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA",
                      "court_body": "1ª Vara Federal Cível (exemplo)", "reference_process": "0000000-00.2014.4.01.3400",
                      "subject": "Exemplo de assunto", "requisition_number": "2025.0000.000.000001",
                      "credit_nature": "alimentar", "legal_preference": "Idosos a partir de 60 anos",
                      "contractual_fees_amount": 50000.0, "res_judicata_date": "25/04/2024",
                      "third_party_interest": False, "reason": "Requisição expedida com valor"}
    opp = {"id": "x", "amount": 300000.0, "potential_score": 0.9, "alias": "api_publica_trf1",
           "requisition_type": "precatorio", "cnj_number": "0000000-00.2024.4.01.3400",
           "beneficiary": "Credor Exemplo", "debtor_entity": "União Federal",
           "document_url": "https://exemplo.invalid/doc", "classification": classification}
    opp.update(extra)
    return opp


def test_alert_follows_pje_header_model():
    slot = datetime(2026, 10, 19, 12, 0)
    msg = worker.format_alert(oficio_opp(), 1, 3, slot)
    lines = msg.splitlines()
    assert lines[0] == "ASPIN | Oportunidade 1 de 3 — entrega das 12h (19/10)"
    for expected in ("Número: 0000000-00.2024.4.01.3400", "Classe: CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA",
                     "Órgão julgador:", "Valor requisitado (principal): R$ 300.000,00", "Processo referência:",
                     "Requisição: Precatório nº 2025.0000.000.000001", "Natureza do crédito: Alimentar",
                     "Preferência legal: Idosos a partir de 60 anos", "Devedor: União Federal",
                     "Honorários contratuais destacados: R$ 50.000,00", "Trânsito em julgado: 25/04/2024",
                     "Terceiro interessado (FIDC/banco/cessionário): não citado"):
        assert expected in msg, expected
    assert len(msg) <= 4000


def test_ranking_prefers_preference_and_penalizes_possible_cession():
    base = oficio_opp()
    ceded = oficio_opp(id="c", amount=500000.0, classification=dict(base["classification"], third_party_interest=True))
    common = oficio_opp(id="m", classification=dict(base["classification"], credit_nature="comum", legal_preference=None))
    ranked = sorted([ceded, common, base], key=worker.rank_score, reverse=True)
    assert [o["id"] for o in ranked] == ["x", "m", "c"]
    assert "SIM — verificar se o crédito já foi cedido" in worker.format_alert(ceded)


def test_llm_normalizes_oficio_fields():
    from app.llm import LLMClassifier
    text = ("Requisição de Pagamento nº 2025.0000.000.000001. Natureza do Crédito: Alimentar. "
            "Valor Total do Beneficiário: R$ 300.000,00. Preferência legal: Idosos a partir de 60 anos.")
    raw = {"is_opportunity": True, "potential_score": 90, "reason": "ok", "requisition_type": "precatorio",
           "evidence_quote": "Valor Total do Beneficiário: R$ 300.000,00", "amount": "300.000,00",
           "amount_basis": "principal", "requisition_number": "2025.0000.000.000001",
           "credit_nature": "Alimentar", "legal_preference": "Idosos a partir de 60 anos",
           "contractual_fees_amount": None, "third_party_interest": "false"}
    result = LLMClassifier()._normalize(raw, text)
    assert result["credit_nature"] == "alimentar" and result["requisition_number"] == "2025.0000.000.000001"
    assert result["third_party_interest"] is False and result["potential_score"] == 0.9
