import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch):
    """Espaçamentos de cortesia às APIs não se aplicam nos testes."""
    monkeypatch.setattr(settings, "datajud_request_delay", 0)


@pytest.fixture(autouse=True)
def _send_message_uses_send_alert(monkeypatch):
    """Nos testes, send_message passa pelo send_alert do módulo (que os testes substituem por mocks)."""
    from app import worker

    async def fake_send_message(text, reply_markup=None, html=False):
        await worker.send_alert(text)
        return {"message_id": 1, "chat_id": str(settings.telegram_chat_id)}
    monkeypatch.setattr(worker, "send_message", fake_send_message)


@pytest.fixture(autouse=True)
def _no_db_for_message_ids(monkeypatch):
    """Sem banco de teste, gravar o message_id do Telegram vira no-op (evita esperas de conexão)."""
    import os
    from app import storage
    if not os.environ.get("ASPIN_TEST_DATABASE_URL"):
        monkeypatch.setattr(storage, "set_telegram_message", lambda *a, **k: None)
