"""Interruptor ENABLE_DATAJUD: desligado, o bot não chama o DataJud em nenhum ponto."""
from unittest.mock import AsyncMock

import pytest

from app import consulta, storage, worker
from app.config import Settings, settings

from test_consulta import CNJ, chat, post  # noqa: F401  (fixture reutilizada)
from test_reliability import oficio_opp


def test_enable_datajud_defaults_to_true_and_reads_env(monkeypatch):
    assert Settings(_env_file=None).enable_datajud is True
    monkeypatch.setenv("ENABLE_DATAJUD", "false")
    assert Settings(_env_file=None).enable_datajud is False


def test_disabled_datajud_needs_no_api_key():
    s = Settings(_env_file=None, enable_datajud=False, datajud_api_key="")
    assert "DATAJUD_API_KEY" not in s.configuration_errors()


def test_signal_is_skipped_when_disabled(monkeypatch):
    called = []
    monkeypatch.setattr(storage, "datajud_signal", lambda cnj: called.append(cnj) or {"published_at": None})
    monkeypatch.setattr(settings, "enable_datajud", False)
    assert worker.datajud_signal("0000000-00.2024.4.01.3400") is None and called == []
    monkeypatch.setattr(settings, "enable_datajud", True)
    assert worker.datajud_signal("0000000-00.2024.4.01.3400") == {"published_at": None}


@pytest.mark.asyncio
async def test_collection_is_skipped_and_recorded(monkeypatch):
    monkeypatch.setattr(settings, "enable_datajud", False)
    collect = AsyncMock()
    monkeypatch.setattr(worker, "collect_expeditions", collect)
    metrics = {"tribunals": 0}
    await worker._collect_datajud(metrics, [])
    assert metrics["datajud_disabled"] is True and collect.await_count == 0


@pytest.mark.asyncio
async def test_digest_does_not_read_datajud_signal_when_disabled(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(settings, "enable_datajud", False)
    state = {}
    monkeypatch.setattr(storage, "get_state", lambda key: state.get(key, {}))
    monkeypatch.setattr(storage, "set_state", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setattr(storage, "pending_alerts", lambda limit: [oficio_opp()])
    monkeypatch.setattr(storage, "datajud_signal", lambda cnj: pytest.fail("DataJud consultado"))
    monkeypatch.setattr(storage, "mark_alerted", lambda i: None)
    monkeypatch.setattr(storage, "archive_unselected", lambda ids, now: 0)
    monkeypatch.setattr(worker, "send_alert", AsyncMock())
    metrics = {"alerts": 0}
    await worker._deliver_digest(metrics, [], datetime(2026, 10, 19, 15, 5, tzinfo=timezone.utc))
    assert metrics["digest"] == "entregue" and metrics["datajud_signal_disabled"] is True


@pytest.mark.asyncio
async def test_processo_command_skips_datajud_when_disabled(monkeypatch, chat):  # noqa: F811
    monkeypatch.setattr(settings, "enable_datajud", False)
    lookup = AsyncMock()
    monkeypatch.setattr(consulta, "datajud_lookup", lookup)
    monkeypatch.setattr(consulta, "djen_lookup", AsyncMock(return_value={"count": 0, "items": [], "requisicao_em": None}))
    monkeypatch.setattr(storage, "process_insight", lambda d: {"opportunity": None, "documents": 0})
    assert await worker.handle_command(post(f"/processo {CNJ}"), set()) is True
    assert lookup.await_count == 0
    assert "DataJud desativada" in chat[-1]["text"] and "Não encontrado no DataJud" not in chat[-1]["text"]
