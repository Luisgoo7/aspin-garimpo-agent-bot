"""Consulta ao DJEN pelo número do processo (usada pelo radar do CRM)."""
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app import sources
from app.config import settings

CNJ_A = "0000001-00.2024.4.01.3400"
A = "00000010020244013400"


def djen_item(i, texto="Intimação das partes", day="2026-10-29"):
    return {"id": i, "hash": f"h{i}", "texto": texto, "data_disponibilizacao": day, "tipoComunicacao": "Intimação"}



class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.params = []

    async def get(self, url, params=None):
        self.params.append(params)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_search_djen_process_queries_by_number(monkeypatch):
    monkeypatch.setattr(sources.asyncio, "sleep", AsyncMock())
    client = FakeClient([FakeResponse(200, {"count": 2, "items": [djen_item(1), djen_item(1), djen_item(2)]})])
    found = await sources.search_djen_process(CNJ_A, date(2026, 10, 1), date(2026, 10, 30), client=client)
    assert found.ok and [r["hash"] for r in found.rows] == ["h1", "h2"]
    params = client.params[0]
    assert params["numeroProcesso"] == A and params["dataDisponibilizacaoInicio"] == "2026-10-01"
    assert params["dataDisponibilizacaoFim"] == "2026-10-30"


@pytest.mark.asyncio
async def test_search_djen_process_retries_and_paginates(monkeypatch):
    monkeypatch.setattr(sources.asyncio, "sleep", AsyncMock())
    page1 = {"count": 101, "items": [djen_item(i) for i in range(100)]}
    page2 = {"count": 101, "items": [djen_item(100)]}
    client = FakeClient([FakeResponse(429), FakeResponse(200, page1), FakeResponse(503), FakeResponse(200, page2)])
    found = await sources.search_djen_process(A, date(2026, 10, 1), date(2026, 10, 30), client=client)
    assert found.ok and len(found.rows) == 101 and [p["pagina"] for p in client.params] == [1, 1, 2, 2]


@pytest.mark.asyncio
async def test_search_djen_process_403_blocks_and_errors_are_not_ok(monkeypatch):
    monkeypatch.setattr(sources.asyncio, "sleep", AsyncMock())
    blocked = await sources.search_djen_process(A, date(2026, 10, 1), date(2026, 10, 30),
                                                client=FakeClient([FakeResponse(403)]))
    assert blocked.blocked and not blocked.ok
    failed = await sources.search_djen_process(A, date(2026, 10, 1), date(2026, 10, 30),
                                               client=FakeClient([FakeResponse(500)] * 3))
    assert not failed.ok and not failed.blocked and failed.errors
    invalid = await sources.search_djen_process("123", date(2026, 10, 1), date(2026, 10, 30), client=FakeClient([]))
    assert not invalid.ok


@pytest.mark.asyncio
async def test_search_djen_process_respects_shared_request_budget(monkeypatch):
    monkeypatch.setattr(sources.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(settings, "djen_max_requests", 2)
    count = [2]
    found = await sources.search_djen_process(A, date(2026, 10, 1), date(2026, 10, 30),
                                              client=FakeClient([]), request_count=count)
    assert found.blocked and count == [2]
