import asyncio
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import sources


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="ok"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def install_http_mock(monkeypatch, responder):
    calls = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            calls.append({"url": url, "params": dict(params or {})})
            answer = responder(dict(params or {}), len(calls))
            if isinstance(answer, FakeResponse):
                return answer
            status_code, payload = answer
            return FakeResponse(status_code, payload)

    monkeypatch.setattr(sources.httpx, "AsyncClient", FakeAsyncClient)
    return calls


def configure_settings(monkeypatch, **overrides):
    values = {
        "database_url": "",
        "djen_page_size": 2,
        "djen_max_pages": 10,
        "djen_max_documents": 2500,
        "djen_max_requests": 80,
        "djen_request_delay": 0,
        "djen_timeout_seconds": 30,
    }
    values.update(overrides)
    monkeypatch.setattr(sources, "settings", SimpleNamespace(**values))


def run_search(*args, **kwargs):
    return asyncio.run(sources.search_djen(*args, **kwargs))


def test_search_djen_fetches_multiple_pages_and_keeps_totals(monkeypatch):
    configure_settings(monkeypatch)
    first_term = sources.DJEN_TERMS[0]

    def responder(params, _call_number):
        if params["texto"] != first_term:
            return 200, {"count": 0, "items": []}
        if params["pagina"] == 1:
            return 200, {
                "count": 3,
                "items": [{"id": "a", "raw": "kept"}, {"id": "b"}],
            }
        return 200, {"count": 3, "items": [{"id": "c"}]}

    calls = install_http_mock(monkeypatch, responder)
    result = run_search(date(2026, 1, 1), date(2026, 1, 1))

    assert isinstance(result, sources.DjenResult)
    assert [row["id"] for row in result.rows] == ["a", "b", "c"]
    assert result.rows[0]["raw"] == "kept"
    assert result.pages == 7  # duas páginas do primeiro termo e cinco vazias
    assert result.totals[f"2026-01-01:{first_term}"] == 3
    assert result.cursor is None
    assert result.limited is False
    assert calls[0]["params"]["texto"] == first_term
    assert calls[0]["params"]["itensPorPagina"] == 2
    assert all(call["params"]["dataDisponibilizacaoInicio"] == "2026-01-01" for call in calls)


def test_search_djen_stops_globally_on_403_with_explicit_error(monkeypatch):
    configure_settings(monkeypatch)
    calls = install_http_mock(monkeypatch, lambda _params, _n: (403, {"items": []}))

    result = run_search(date(2026, 1, 1), date(2026, 1, 1))

    assert result.errors == ["djen_http_403: acesso negado/origem geográfica"]
    assert len(calls) == 1
    assert result.limited is True
    assert result.cursor == {
        "since": "2026-01-01",
        "until": "2026-01-01",
        "date_index": 0,
        "term_index": 0,
        "page": 1,
    }


def test_search_djen_retries_once_when_count_positive_but_items_empty(monkeypatch):
    configure_settings(monkeypatch)
    calls = install_http_mock(monkeypatch, lambda _params, _n: (200, {"count": 1, "items": []}))

    result = run_search(date(2026, 1, 1), date(2026, 1, 1))

    assert len(calls) == 2
    assert any("djen_empty_page_count_gt_zero" in error for error in result.errors)
    assert result.limited is True
    assert result.cursor["page"] == 1


def test_search_djen_deduplicates_ids_and_fallback_hash_without_losing_raw_fields(monkeypatch):
    configure_settings(monkeypatch)
    first_term = sources.DJEN_TERMS[0]
    fallback = {
        "texto": "mesmo texto",
        "siglaTribunal": "TJSP",
        "numeroProcesso": "0001",
        "dataDisponibilizacao": "2026-01-01",
        "extra": "preserve",
    }

    def responder(params, _call_number):
        if params["texto"] != first_term:
            return 200, {"count": 0, "items": []}
        if params["pagina"] == 1:
            return 200, {"count": 3, "items": [fallback, {"numeroComunicacao": "nc-1"}]}
        return 200, {"count": 3, "items": [dict(fallback, extra="duplicate")]}

    install_http_mock(monkeypatch, responder)
    result = run_search(date(2026, 1, 1), date(2026, 1, 1))

    assert len(result.rows) == 2
    assert result.rows[0]["extra"] == "preserve"
    assert all(sources._row_identity(row) != "None" for row in result.rows)


def test_document_cap_returns_whole_page_and_cursor_resumes_next_page(monkeypatch):
    configure_settings(monkeypatch, djen_max_documents=2)
    first_term = sources.DJEN_TERMS[0]

    def responder(params, _call_number):
        if params["texto"] != first_term:
            return 200, {"count": 0, "items": []}
        pages = {
            1: [{"id": "p1"}, {"id": "p2"}],
            2: [{"id": "p3"}, {"id": "p4"}],
        }
        return 200, {"count": 4, "items": pages.get(params["pagina"], [])}

    first_calls = install_http_mock(monkeypatch, responder)
    first = run_search(date(2026, 1, 1), date(2026, 1, 1))

    assert [row["id"] for row in first.rows] == ["p1", "p2"]
    assert first.limited is True
    assert first.cursor == {
        "since": "2026-01-01",
        "until": "2026-01-01",
        "date_index": 0,
        "term_index": 0,
        "page": 2,
    }
    assert first_calls[-1]["params"]["pagina"] == 1

    # O próximo ciclo parte diretamente da página 2, sem repetir a primeira.
    configure_settings(monkeypatch)
    second_calls = install_http_mock(monkeypatch, responder)
    second = run_search(date(2026, 1, 1), date(2026, 1, 1), first.cursor)

    assert second_calls[0]["params"]["pagina"] == 2
    resumed_term_calls = [
        call for call in second_calls if call["params"]["texto"] == first_term
    ]
    assert [call["params"]["pagina"] for call in resumed_term_calls] == [2]
    assert [row["id"] for row in second.rows] == ["p3", "p4"]
    assert second.cursor is None
    assert second.limited is False


def test_max_pages_is_per_cycle_and_cursor_preserves_original_window(monkeypatch):
    configure_settings(monkeypatch, djen_max_pages=1)
    first_term = sources.DJEN_TERMS[0]

    def responder(params, _call_number):
        if params["texto"] == first_term:
            return 200, {"count": 3, "items": [{"id": f"p{params['pagina']}"}] * 2}
        return 200, {"count": 0, "items": []}

    install_http_mock(monkeypatch, responder)
    result = run_search(date(2026, 1, 1), date(2026, 1, 2))

    assert result.limited is True
    assert result.cursor["since"] == "2026-01-01"
    assert result.cursor["until"] == "2026-01-02"
    assert result.cursor["page"] == 2
