"""Notificação enxuta, botões Aprovar/Descartar e envio à planilha."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app import storage, worker
from app.config import settings

from test_reliability import oficio_opp

HEADERS = list(worker.SHEET_HEADERS)


def test_compact_alert_is_short_and_clean():
    msg = worker.format_alert_compact(oficio_opp(), 2, 3, datetime(2026, 10, 19, 18))
    lines = msg.splitlines()
    assert lines[0] == "<b>💼 Oportunidade 2/3 · 18h</b>"
    assert lines[1] == "<b>R$ 300.000,00</b> · Precatório · TRF1"
    assert "Alimentar" in lines and "Idosos" not in msg  # preferência não entra na mensagem (critério da cliente)
    assert "Credor Exemplo → União Federal" in msg
    assert len(lines) <= 9 and "Honorários" not in msg  # detalhes vão para a planilha
    ceded = oficio_opp(classification=dict(oficio_opp()["classification"], third_party_interest=True))
    assert "verificar cessão" in worker.format_alert_compact(ceded)


def test_compact_alert_escapes_html():
    msg = worker.format_alert_compact(oficio_opp(beneficiary="<script>x</script> & Cia"))
    assert "&lt;script&gt;" in msg and "&amp; Cia" in msg


def test_sheet_row_has_every_column_of_the_spreadsheet():
    row = worker.sheet_row(dict(oficio_opp(), decided_by="Ana", decided_at=datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)))
    assert list(row) == HEADERS + [worker.SHEET_PROCESS_LINK]
    assert row["Aprovado em"] == "19/10/2026 12:00" and row["Valor (R$)"] == 300000.0
    assert row["Natureza"] == "Alimentar" and row["Comissão estimada (R$)"] == 15000.0
    assert row["Nota ASPIN (0-10)"] == 7 and row["Confiança IA"] == 0.9  # sem DataJud e sem data de publicação
    assert "Natureza alimentar" in row["Critérios atendidos"] and "DataJud" not in row["Critérios atendidos"]


def test_sheet_row_links_the_process_to_the_cnj_public_search():
    row = worker.sheet_row(dict(oficio_opp(), cnj_number="00220908220188260224", document_url="https://www.dje.tjsp.jus.br"))
    link = "https://comunica.pje.jus.br/consulta?numeroProcesso=00220908220188260224"
    assert row[worker.SHEET_PROCESS_LINK] == link
    assert row["Fonte"] == link  # página inicial do diário não é link da publicação
    doc = "https://pje1g.trf3.jus.br:443/pje/Processo/ConsultaDocumento/listView.seam?x=261014"
    assert worker.sheet_row(dict(oficio_opp(), document_url=doc))["Fonte"] == doc
    assert worker.process_url("123") == ""


def test_apps_script_uses_the_same_columns_as_the_spreadsheet():
    script = open("docs/planilha_apps_script.gs", encoding="utf-8").read()
    start = script.index("const HEADERS = [")
    block = script[start:script.index("];", start)]
    import re
    assert re.findall(r"'([^']+)'", block) == HEADERS


class ButtonsStore:
    def __init__(self):
        self.state = {}
        self.decisions = {}
        self.synced = []
        self.sheet_errors = []

    def install(self, monkeypatch):
        monkeypatch.setattr(storage, "get_state", lambda key: self.state.get(key, {}))
        monkeypatch.setattr(storage, "set_state", lambda key, value: self.state.__setitem__(key, value))
        monkeypatch.setattr(storage, "datajud_signal", lambda cnj: None)
        monkeypatch.setattr(storage, "record_decision", self.record)
        monkeypatch.setattr(storage, "sheet_outbox", lambda limit=50: [
            dict(oficio_opp(id=i), decided_by=d[1], decided_at=datetime(2026, 10, 19, 15, tzinfo=timezone.utc), decision=d[0])
            for i, d in self.decisions.items() if d[0] == "aprovada" and i not in self.synced][:limit])
        monkeypatch.setattr(storage, "mark_sheet",
                            lambda i, error=None: self.sheet_errors.append(error) if error else self.synced.append(i))

    def record(self, opp_id, decision, by):
        created = opp_id not in self.decisions
        if created:
            self.decisions[opp_id] = (decision, by)
        d = self.decisions[opp_id]
        return created, {"id": opp_id, "decision": d[0], "decided_by": d[1]}


def click(update_id, data, user="Ana", chat=None, msg=10):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data, "from": {"id": 7, "first_name": user},
        "message": {"message_id": msg, "chat": {"id": chat or settings.telegram_chat_id}}}}


@pytest.fixture
def telegram_fakes(monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_id", "-100123")
    monkeypatch.setattr(settings, "telegram_approvers", "")
    calls = {"answers": [], "buttons": [], "rows": []}
    monkeypatch.setattr(worker, "answer_callback", AsyncMock(side_effect=lambda cid, text: calls["answers"].append(text)))
    monkeypatch.setattr(worker, "set_buttons",
                        AsyncMock(side_effect=lambda c, m, k: calls["buttons"].append(k["inline_keyboard"][0][0]["text"])))

    async def append(row):
        calls["rows"].append(row)
        return True
    monkeypatch.setattr(worker.sheets, "append_row", append)
    monkeypatch.setattr(worker.sheets, "configured", lambda: True)
    return calls


@pytest.mark.asyncio
async def test_approve_click_records_decision_updates_button_and_writes_sheet(monkeypatch, telegram_fakes):
    store = ButtonsStore()
    store.install(monkeypatch)
    monkeypatch.setattr(worker, "get_updates", AsyncMock(return_value=[click(5, "a:opp1"), click(6, "d:opp2")]))
    result = await worker.process_buttons()
    assert result["approved"] == 1 and result["discarded"] == 1 and result["sheet_ok"] == 1
    assert telegram_fakes["buttons"] == ["✅ Aprovado por Ana", "❌ Descartado por Ana"]
    assert [r["ID interno"] for r in telegram_fakes["rows"]] == ["opp1"]
    assert store.state["telegram_updates"] == {"offset": 7}


@pytest.mark.asyncio
async def test_double_click_and_conflicting_click_keep_first_decision(monkeypatch, telegram_fakes):
    store = ButtonsStore()
    store.install(monkeypatch)
    monkeypatch.setattr(worker, "get_updates", AsyncMock(return_value=[
        click(1, "a:opp1"), click(2, "a:opp1"), click(3, "d:opp1", user="Bruno")]))
    result = await worker.process_buttons()
    assert result["approved"] == 1 and result["discarded"] == 0
    assert store.decisions["opp1"] == ("aprovada", "Ana")
    assert telegram_fakes["answers"][1].startswith("Já decidido") and len(telegram_fakes["rows"]) == 1


@pytest.mark.asyncio
async def test_clicks_from_other_chats_and_unauthorized_users_are_ignored(monkeypatch, telegram_fakes):
    store = ButtonsStore()
    store.install(monkeypatch)
    monkeypatch.setattr(settings, "telegram_approvers", "999")
    monkeypatch.setattr(worker, "get_updates", AsyncMock(return_value=[click(1, "a:opp1", chat="-1"), click(2, "a:opp1")]))
    result = await worker.process_buttons()
    assert result["ignored"] == 2 and not store.decisions


@pytest.mark.asyncio
async def test_sheet_failure_keeps_approval_in_outbox_until_it_works(monkeypatch, telegram_fakes):
    store = ButtonsStore()
    store.install(monkeypatch)
    monkeypatch.setattr(worker, "get_updates", AsyncMock(return_value=[click(1, "a:opp1")]))

    async def broken(row):
        raise worker.sheets.SheetsError("planilha: HTTP 500")
    monkeypatch.setattr(worker.sheets, "append_row", broken)
    first = await worker.process_buttons()
    assert first["sheet_failed"] == 1 and store.synced == [] and store.decisions["opp1"][0] == "aprovada"
    assert worker.buttons_exit_code(first) == 0  # tolerado; vira vermelho só se persistir
    monkeypatch.setattr(worker, "get_updates", AsyncMock(return_value=[]))

    async def ok(row):
        return True
    monkeypatch.setattr(worker.sheets, "append_row", ok)
    second = await worker.process_buttons()
    assert second["sheet_ok"] == 1 and store.synced == ["opp1"]


def test_persistent_sheet_failure_turns_red(monkeypatch):
    state = {}
    monkeypatch.setattr(storage, "get_state", lambda key: state.get(key, {}))
    monkeypatch.setattr(storage, "set_state", lambda key, value: state.__setitem__(key, value))
    codes = [worker.buttons_exit_code({"sheet_failed": 1, "sheet_error": "x"}) for _ in range(3)]
    assert codes == [0, 0, worker.EXIT_DELIVERY_FAILED]
    assert worker.buttons_exit_code({"sheet_ok": 1}) == 0 and state["sheet_failures"] == {}


@pytest.mark.asyncio
async def test_database_error_does_not_lose_the_click(monkeypatch, telegram_fakes):
    store = ButtonsStore()
    store.install(monkeypatch)

    def boom(*a):
        raise RuntimeError("db fora")
    monkeypatch.setattr(storage, "record_decision", boom)
    monkeypatch.setattr(worker, "get_updates", AsyncMock(return_value=[click(41, "a:opp1"), click(42, "a:opp2")]))
    await worker.process_buttons()
    assert store.state["telegram_updates"] == {"offset": 41}  # o mesmo clique volta no próximo ciclo


@pytest.mark.asyncio
async def test_sheet_not_configured_keeps_approvals_pending(monkeypatch, telegram_fakes):
    store = ButtonsStore()
    store.install(monkeypatch)
    monkeypatch.setattr(worker.sheets, "configured", lambda: False)
    monkeypatch.setattr(worker, "get_updates", AsyncMock(return_value=[click(1, "a:opp1")]))
    result = await worker.process_buttons()
    assert result["sheet_not_configured"] and result["sheet_pending"] == 1 and store.synced == []


@pytest.mark.asyncio
async def test_sheets_client_follows_apps_script_redirect(monkeypatch):
    from app import sheets
    monkeypatch.setattr(settings, "sheets_webhook_url", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setattr(settings, "sheets_token", "t")
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            seen["payload"] = json
            return Response()
    monkeypatch.setattr(sheets.httpx, "AsyncClient", Client)
    assert await sheets.append_row({"ID interno": "1"}) is True
    assert seen["follow_redirects"] is True and seen["payload"] == {"token": "t", "row": {"ID interno": "1"}}


@pytest.mark.parametrize("lease,last_digest,minutes,utc,expected", [
    ("worker (expira em 900s)", {}, 200, "2026-10-19T15:10:00", None),          # ciclo rodando
    (None, {}, 10, "2026-10-19T15:10:00", "entrega das 12h pendente"),           # 12h ainda não entregue
    (None, {"slot": "2026-10-19T12:00:00-03:00"}, 10, "2026-10-19T15:10:00", None),
    (None, {"slot": "2026-10-19T12:00:00-03:00"}, 90, "2026-10-19T15:10:00", "coleta atrasada"),
    (None, {}, None, "2026-10-19T20:00:00", "nenhum ciclo registrado"),      # 17h: fora da janela de entrega
])
def test_watchdog_decides_when_to_dispatch(monkeypatch, lease, last_digest, minutes, utc, expected):
    monkeypatch.setattr(storage, "lease_holder", lambda: lease)
    monkeypatch.setattr(storage, "get_state", lambda key: last_digest if key == "last_digest" else {})
    monkeypatch.setattr(storage, "minutes_since_last_cycle", lambda: minutes)
    now = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)
    assert worker.collector_dispatch_reason(now) == expected


@pytest.mark.asyncio
async def test_dispatch_uses_github_api_with_job_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "dono/repo")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    seen = {}

    class Response:
        status_code = 204

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            seen.update(url=url, auth=headers["Authorization"], body=json)
            return Response()
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert await worker.dispatch_collector("teste") is True
    assert seen["url"].endswith("/repos/dono/repo/actions/workflows/garimpo.yml/dispatches")
    assert seen["auth"] == "Bearer tok" and seen["body"] == {"ref": "main"}



def test_nota_aspin_is_explainable():
    recent = dict(oficio_opp()["classification"], published_at=datetime.now(timezone.utc).isoformat())
    nota, items = worker.nota_aspin(dict(oficio_opp(classification=recent), datajud_signal={"published_at": None}))
    assert nota == 10 and all(ok for _, _, ok, _ in items)
    ceded = dict(oficio_opp(classification=dict(oficio_opp()["classification"], third_party_interest=True,
                                                 credit_nature="comum", legal_preference=None)))
    assert worker.nota_aspin(ceded)[0] == 4  # valor + expedido
    msg = worker.format_alert_compact(oficio_opp())
    assert "⭐ <b>Nota ASPIN 7/10</b>" in msg.splitlines()
    assert "IA 90%" not in msg and "✅" not in msg and "▫️" not in msg  # notificação mostra só a nota


@pytest.mark.asyncio
async def test_format_is_never_sent_to_old_script(monkeypatch):
    from app import sheets
    monkeypatch.setattr(settings, "sheets_webhook_url", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setattr(settings, "sheets_token", "t")
    posts = []
    monkeypatch.setattr(sheets, "script_version", AsyncMock(return_value=sheets.LAYOUT_VERSION - 1))
    monkeypatch.setattr(sheets, "_post", AsyncMock(side_effect=lambda *a, **k: posts.append(a) or True))
    with pytest.raises(sheets.SheetsError, match="desatualizado"):
        await sheets.format_sheet()
    assert posts == []
    monkeypatch.setattr(sheets, "script_version", AsyncMock(return_value=sheets.LAYOUT_VERSION))
    assert await sheets.format_sheet() is True and posts[0][0]["action"] == "format"


@pytest.mark.asyncio
async def test_layout_is_formatted_once_per_version(monkeypatch, telegram_fakes):
    store = ButtonsStore()
    store.install(monkeypatch)
    fmt = AsyncMock(return_value=True)
    monkeypatch.setattr(worker.sheets, "format_sheet", fmt)
    monkeypatch.setattr(worker, "get_updates", AsyncMock(return_value=[]))
    first = await worker.process_buttons()
    second = await worker.process_buttons()
    assert first.get("sheet_formatted") and not second.get("sheet_formatted") and fmt.await_count == 1



def test_client_criteria_total_amount_and_dates():
    from app.llm import LLMClassifier
    text = ("Valor Total do Beneficiário: R$ 338.813,79. VALOR TOTAL REQUISITADO (R$): 423.517,23. "
            "Data de Cadastro da Req: 05/11/2025. Data base 01/08/2025. Honorários R$ 84.703,44.")
    raw = {"is_opportunity": True, "potential_score": 0.9, "reason": "ok", "requisition_type": "precatorio",
           "evidence_quote": "VALOR TOTAL REQUISITADO (R$): 423.517,23", "amount": "423.517,23",
           "amount_basis": "total", "issued_at": "05/11/2025", "base_date": "01/08/2025",
           "contractual_fees_amount": "84.703,44", "third_party_interest": False}
    result = LLMClassifier()._normalize(raw, text)
    assert result["amount"] == 423517.23 and result["amount_basis"] == "total"
    assert result["issued_at"] == "05/11/2025" and result["base_date"] == "01/08/2025"
    row = worker.sheet_row(dict(oficio_opp(), classification=dict(oficio_opp()["classification"], **result)))
    assert row["Data de expedição (ofício)"] == "05/11/2025" and row["Data base"] == "01/08/2025"


def test_preference_no_longer_boosts_ranking():
    base = oficio_opp()
    without = oficio_opp(id="sem", classification=dict(base["classification"], legal_preference=None))
    assert worker.rank_score(base) == worker.rank_score(without)


def test_minimum_ticket_is_150k():
    from app.config import Settings
    assert Settings(_env_file=None).min_opportunity_amount == 150000
