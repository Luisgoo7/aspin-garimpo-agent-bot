import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch):
    """Espaçamentos de cortesia às APIs não se aplicam nos testes."""
    monkeypatch.setattr(settings, "datajud_request_delay", 0)
