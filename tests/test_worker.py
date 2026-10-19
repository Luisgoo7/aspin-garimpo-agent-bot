import asyncio
import os
from datetime import date, datetime, timezone
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import storage, worker
from app.config import Settings, settings
from app.opportunity import canonical_alias, canonical_cnj, keyword_score, parse_amount


@pytest.mark.parametrize("text", ["RPV", "RPV expedido", "Precatório", "Ofício requisitório", "REQUISIÇÃO DE PAGAMENTO"])
def test_keyword_variants(text):
    assert keyword_score(text) > 0.25


@pytest.mark.parametrize("value,expected", [("R$ 135.654,57", 135654.57), ("135.654", 135654), (135654.57, 135654.57), ("NaN", None), (True, None), ("-12", None)])
def test_amounts(value, expected):
    assert parse_amount(value) == expected


def test_normalized_identity():
    assert canonical_alias("TRF1") == canonical_alias("api_publica_trf1")
    assert canonical_cnj("1002747-62.2024.4.01.3600") == "10027476220244013600"


def test_configuration_is_not_success_without_secrets():
    config = Settings(_env_file=None, database_url="", telegram_bot_token="", telegram_chat_id="", groq_api_key="", gemini_api_key="", nvidia_api_key="", datajud_api_key="")
    errors = config.configuration_errors()
    assert "DATABASE_URL" in errors
    assert "DATAJUD_API_KEY" in errors
    assert any("GROQ_API_KEY" in x for x in errors)


def test_status_distinguishes_failures():
    msg = worker.status_message({"classified": 0, "queue": {}}, ["djen_http_403"])
    assert "Coleta com falhas" in msg
    assert "djen_http_403" in msg
    assert "Nenhuma oportunidade válida" not in msg


def test_status_distinguishes_backlog():
    msg = worker.status_message({"queue": {"pending": 15}}, [])
    assert "Coleta parcial" in msg
    assert "Fila pendente: 15" in msg


def test_alert_does_not_mislabel_gross_as_net():
    msg = worker.format_alert({"amount": 135654.57, "potential_score": 0.9, "alias": "api_publica_trf1", "classification": {"amount_basis": "principal"}})
    assert "Principal" in msg
    assert "Líquido" not in msg
    assert "não retorno garantido" in msg


@pytest.mark.asyncio
async def test_ai_failure_keeps_document_for_retry(monkeypatch):
    doc = {"id": "doc", "processing_attempts": 1, "text_excerpt": "RPV expedida", "raw_data": {}}
    queue = iter([doc, None])
    finished = []
    monkeypatch.setattr(storage, "claim_document", lambda: next(queue))
    monkeypatch.setattr(storage, "finish_document", lambda d, status, *args, **kwargs: finished.append(status))
    monkeypatch.setattr(worker.LLMClassifier, "classify", AsyncMock(return_value={"error": True, "reason": "groq: HTTP 429"}))
    metrics = {"classified": 0, "opportunities": 0, "below_minimum": 0}
    errors = []
    await worker._process_queue(metrics, errors)
    assert finished == ["retry"]
    assert errors and metrics["classified"] == 1


@pytest.mark.asyncio
async def test_pending_telegram_recovers_after_failure(monkeypatch):
    opp = {"id": "opp", "amount": 120000, "potential_score": 0.95, "classification": {}}
    marked = []
    monkeypatch.setattr(storage, "pending_alerts", lambda limit: [opp])
    monkeypatch.setattr(storage, "mark_alerted", marked.append)
    monkeypatch.setattr(worker, "send_alert", AsyncMock(side_effect=RuntimeError("falha")))
    metrics = {"alerts": 0}
    errors = []
    await worker._send_pending_alerts(metrics, errors)
    assert not marked and errors
    monkeypatch.setattr(worker, "send_alert", AsyncMock())
    await worker._send_pending_alerts(metrics, [])
    assert marked == ["opp"] and metrics["alerts"] == 1


@pytest.mark.asyncio
async def test_poll_once_fails_on_collection_errors(monkeypatch):
    monkeypatch.setattr(worker, "run_cycle", AsyncMock(return_value={"outcome": "source_failed", "opportunities": 0, "errors": ["datajud indisponível"]}))
    with pytest.raises(worker.CycleFailure):
        await worker.poll_once()


@pytest.fixture
def test_db(monkeypatch):
    url = os.environ.get("ASPIN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Banco isolado não solicitado")
    if "aspin_test" not in url:
        pytest.fail("Somente banco aspin_test pode ser usado pelos testes destrutivos")
    monkeypatch.setattr(settings, "database_url", url)
    storage.ensure_schema()
    with storage.connect() as conn:
        conn.execute("truncate public.opportunities,public.evidence,public.processes,public.source_documents,public.collection_runs,public.audit_log,public.bot_state,public.collector_lease cascade")
    return url


def source_row():
    return {"id": "fixture", "siglaTribunal": "TRF1", "numero_processo": "1002747-62.2024.4.01.3600", "texto": "Requisitem-se R$ 135.654,57. Vista do ofício requisitório.", "data_disponibilizacao": date.today().isoformat()}


def positive():
    return {"is_opportunity": True, "amount": 135654.57, "amount_basis": "principal", "potential_score": 0.95, "requisition_type": "oficio_requisitorio", "evidence_quote": "Requisitem-se R$ 135.654,57.", "reason": "Ordem judicial expressa"}


def test_migration_is_additive_idempotent(test_db):
    storage.upsert_document(source_row())
    storage.ensure_schema()
    assert storage.queue_counts()["pending"] == 1


def test_retry_does_not_disappear_when_document_is_seen_again(test_db):
    row = source_row()
    storage.upsert_document(row)
    doc = storage.claim_document()
    storage.finish_document(doc, "retry", error="IA offline")
    storage.upsert_document(row)
    assert storage.queue_counts()["retry"] == 1
    with storage.connect() as conn:
        conn.execute("update public.source_documents set next_attempt_at=now()-interval '1 minute'")
    reclaimed = storage.claim_document()
    assert reclaimed["processing_attempts"] == 2


def test_source_changes_are_reprocessed(test_db):
    row = source_row()
    storage.upsert_document(row)
    storage.finish_document(storage.claim_document(), "rejected", {"is_opportunity": False})
    row["texto"] += " Publicação retificada."
    storage.upsert_document(row)
    assert storage.queue_counts()["pending"] == 1


def test_duplicate_sources_do_not_create_duplicate_opportunities(test_db):
    storage.upsert_document(source_row())
    doc = storage.claim_document()
    a, first = storage.save_opportunity(doc, positive())
    doc["tribunal_alias"] = "trf1"
    b, second = storage.save_opportunity(doc, positive())
    assert first and not second and a == b
    assert len(storage.pending_alerts()) == 1
    storage.mark_alerted(a)
    assert storage.pending_alerts() == []


def test_stale_processing_is_recovered(test_db):
    storage.upsert_document(source_row())
    storage.claim_document()
    with storage.connect() as conn:
        conn.execute("update public.source_documents set processing_started_at=now()-interval '1 hour'")
    assert storage.claim_document()["processing_attempts"] == 2


def test_advisory_lock_prevents_two_collectors(test_db):
    with storage.collector_lock() as first:
        with storage.collector_lock() as second:
            assert first and not second
    with storage.collector_lock() as third:
        assert third  # liberado ao sair


def test_expired_lease_from_crashed_collector_is_taken_over(test_db):
    with storage.connect() as conn:
        conn.execute("insert into public.collector_lease(name,holder,expires_at) values('garimpo','morto',now()-interval '1 minute')")
    with storage.collector_lock() as locked:
        assert locked


def test_live_lease_blocks_second_collector(test_db):
    with storage.connect() as conn:
        conn.execute("insert into public.collector_lease(name,holder,expires_at) values('garimpo','aspin-garimpo-worker:1:x',now()+interval '10 minutes')")
    with storage.collector_lock() as locked:
        assert not locked
    assert storage.lease_holder().startswith("aspin-garimpo-worker")


def test_check_connection_reports_recent_runs(test_db):
    run = storage.start_run(["djen"])
    storage.finish_run(run, "success", {}, [])
    text = storage.check_connection()
    assert "conexão=ok" in text and "success" in text


def test_metrics_are_persisted(test_db):
    id = storage.start_run()
    storage.finish_run(id, "degraded", {"classified": 3, "alerts": 0}, ["IA offline"])
    with storage.connect() as conn:
        row = conn.execute("select * from public.collection_runs where id=%s", (id,)).fetchone()
    assert row["outcome"] == "degraded" and row["errors"] == ["IA offline"]


@pytest.mark.asyncio
async def test_end_to_end_real_database_mocked_external_services(test_db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "token-de-teste")
    monkeypatch.setattr(settings, "telegram_chat_id", "chat-de-teste")
    monkeypatch.setattr(settings, "groq_api_key", "chave-de-teste")
    monkeypatch.setattr(settings, "enable_datajud", False)
    monkeypatch.setattr(settings, "enable_tjsp_probe", False)
    batch = SimpleNamespace(rows=[source_row()], pages=1, totals={"teste": 1}, limited=False, errors=[], cursor=None)
    monkeypatch.setattr(worker, "search_djen", AsyncMock(return_value=batch))
    monkeypatch.setattr(worker.LLMClassifier, "classify", AsyncMock(return_value=positive()))
    sender = AsyncMock()
    from datetime import datetime as _dt, timezone as _tz
    monkeypatch.setattr(worker, "digest_slot_due", lambda now: _dt(2026, 10, 19, 12, tzinfo=_tz.utc))
    monkeypatch.setattr(worker, "send_alert", sender)
    result = await worker.run_cycle(120)
    assert result["outcome"] == "success"
    assert result["opportunities"] == 1 and result["alerts"] == 1
    assert sender.await_count == 1  # só a oportunidade; status vai ao canal apenas em falha
    assert storage.pending_alerts() == []
    result2 = await worker.run_cycle(120)
    assert result2["opportunities"] == 0 and result2["alerts"] == 0
    assert sender.await_count == 1  # mesmo horário de entrega: nada reenviado


def test_existing_unalerted_record_can_be_enriched(test_db):
    storage.upsert_document(source_row())
    doc=storage.claim_document()
    id,created=storage.save_opportunity(doc,positive())
    with storage.connect() as conn:
        conn.execute("update public.opportunities set amount=null,potential_score=0.5,status='review' where id=%s",(id,))
        conn.execute("update public.processes set cnj_number='1002747-62.2024.4.01.3600'")
    enriched,new=storage.save_opportunity(doc,positive())
    assert enriched==id and not new
    assert float(storage.pending_alerts()[0]['amount'])==135654.57


@pytest.mark.asyncio
async def test_below_minimum_document_does_not_call_ai(monkeypatch):
    doc={'id':'doc','processing_attempts':1,'text_excerpt':'RPV expedida no valor de R$ 5.000,00','raw_data':{}}
    docs=iter([doc,None])
    monkeypatch.setattr(storage,'claim_document',lambda:next(docs))
    finished=[]
    monkeypatch.setattr(storage,'finish_document',lambda d,status,*args,**kwargs:finished.append(status))
    classifier=AsyncMock()
    monkeypatch.setattr(worker.LLMClassifier,'classify',classifier)
    metrics={'classified':0,'opportunities':0,'below_minimum':0}
    await worker._process_queue(metrics,[])
    assert finished==['below_minimum']
    assert metrics['below_minimum']==1 and metrics['classified']==0
    classifier.assert_not_awaited()



def test_pending_alerts_are_ranked_by_expected_value(test_db):
    storage.upsert_document(source_row())
    doc = storage.claim_document()
    low = dict(positive(), amount=150000.0, potential_score=0.95, requisition_type="rpv")
    high = dict(positive(), amount=900000.0, potential_score=0.80)
    storage.save_opportunity(doc, low)
    storage.save_opportunity(doc, high)
    ranked = storage.pending_alerts(3)
    assert [float(o["amount"]) for o in ranked] == [900000.0, 150000.0]
    archived = storage.archive_unselected([ranked[0]["id"]], datetime.now(timezone.utc))
    assert archived == 1 and len(storage.pending_alerts(3)) == 1
