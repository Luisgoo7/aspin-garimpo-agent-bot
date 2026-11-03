"""Leitura do CRM pela ação crm_export do script da planilha."""
from unittest.mock import AsyncMock

import httpx
import pytest

from app import sheets
from app.config import settings


class FakeResponse:
    def __init__(self, status=200, payload=None, json_error=False):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("html")
        return self._payload


def fake_client(posts: list, response=None, error=None):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            posts.append(json)
            if error:
                raise error
            return response
    return Client


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "sheets_webhook_url", "https://script.google.com/macros/s/segredo/exec")
    monkeypatch.setattr(settings, "sheets_token", "tok")


@pytest.mark.asyncio
async def test_crm_export_is_never_sent_to_old_script(monkeypatch, configured):
    posts = []
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(posts, FakeResponse(200, {"ok": True, "rows": []})))
    for old in (0, 1, 5, 6):
        monkeypatch.setattr(sheets, "script_version", AsyncMock(return_value=old))
        assert await sheets.crm_export() is None
    assert posts == []  # o script antigo gravaria o pedido como linha em branco


@pytest.mark.asyncio
async def test_crm_export_returns_rows_when_script_is_v7(monkeypatch, configured):
    posts = []
    payload = {"ok": True, "rows": [{"id": "1", "etapa": "Nova"}, "lixo"], "stages": ["Nova"], "appUrl": "https://x"}
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(posts, FakeResponse(200, payload)))
    monkeypatch.setattr(sheets, "script_version", AsyncMock(return_value=7))
    data = await sheets.crm_export()
    assert posts == [{"token": "tok", "action": "crm_export"}]
    assert data["rows"] == [{"id": "1", "etapa": "Nova"}] and data["appUrl"] == "https://x"


@pytest.mark.asyncio
async def test_crm_export_not_configured_is_skipped(monkeypatch):
    monkeypatch.setattr(settings, "sheets_webhook_url", "")
    version = AsyncMock(return_value=7)
    monkeypatch.setattr(sheets, "script_version", version)
    assert await sheets.crm_export() is None and version.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("response,error,match", [
    (FakeResponse(200, {"ok": False, "error": "acesso negado"}), None, "recusada"),
    (FakeResponse(500, {}), None, "HTTP 500"),
    (FakeResponse(200, json_error=True), None, "não é JSON"),
    (FakeResponse(200, {"ok": True}), None, "sem a lista"),
    (None, httpx.ConnectError("falhou em https://script.google.com/macros/s/segredo/exec"), "rede"),
])
async def test_crm_export_errors_are_sanitized(monkeypatch, configured, response, error, match):
    monkeypatch.setattr(httpx, "AsyncClient", fake_client([], response, error))
    monkeypatch.setattr(sheets, "script_version", AsyncMock(return_value=7))
    with pytest.raises(sheets.SheetsError, match=match) as info:
        await sheets.crm_export()
    assert "segredo" not in str(info.value) and "tok" not in str(info.value)


@pytest.mark.asyncio
async def test_append_row_still_works_after_refactor(monkeypatch, configured):
    posts = []
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(posts, FakeResponse(200, {"ok": True})))
    assert await sheets.append_row({"ID interno": "1"}) is True
    assert posts == [{"token": "tok", "row": {"ID interno": "1"}}]
