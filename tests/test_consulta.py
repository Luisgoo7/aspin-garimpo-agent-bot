"""Comando /processo e cobertura de todos os tribunais."""
from unittest.mock import AsyncMock

import pytest

from app import consulta, storage, worker
from app.config import DEFAULT_DATAJUD_TRIBUNALS, Settings, settings

CNJ = "1075810-41.2024.4.01.3400"  # número público do ofício de exemplo
DIGITS = "10758104120244013400"


def test_all_state_and_federal_courts_are_covered():
    aliases = DEFAULT_DATAJUD_TRIBUNALS.split(",")
    assert len(aliases) == 33 and len(set(aliases)) == 33
    assert "api_publica_tjdft" in aliases and "api_publica_trf6" in aliases and aliases[0] == "api_publica_tjsp"
    assert Settings(_env_file=None, datajud_tribunals="todos").datajud_tribunals == DEFAULT_DATAJUD_TRIBUNALS
    assert Settings(_env_file=None, datajud_tribunals="api_publica_tjsp").datajud_tribunals == "api_publica_tjsp"


@pytest.mark.parametrize("number,alias", [
    (CNJ, "api_publica_trf1"),
    ("5002302-88.2024.4.03.6002", "api_publica_trf3"),
    ("0812885-42.2017.8.20.5124", "api_publica_tjrn"),
    ("0000001-23.2026.8.07.0001", "api_publica_tjdft"),
    ("0000001-23.2026.5.02.0001", "api_publica_trt2"),
    ("0000001-23.2026.1.00.0000", None),  # STF não integra o DataJud
])
def test_alias_is_derived_from_cnj_number(number, alias):
    assert consulta.alias_from_cnj(number) == alias


def test_cnj_check_digits():
    assert consulta.cnj_check_digits_ok(CNJ)
    assert not consulta.cnj_check_digits_ok("1075810-42.2024.4.01.3400")
    assert consulta.format_cnj(DIGITS) == CNJ


def test_brasilia_dates_are_not_shifted():
    when = consulta.parse_datahora("20260710143000")
    assert when.strftime("%H:%M") == "14:30" and when.utcoffset().total_seconds() == -3 * 3600


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def search(self, alias, query, http=None):
        self.calls += 1
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_partial_response_is_unavailable_not_not_found(monkeypatch):
    monkeypatch.setattr(consulta.asyncio, "sleep", AsyncMock())
    partial = {"_shards": {"failed": 2}, "hits": {"total": {"value": 0}, "hits": []}}
    client = FakeClient([partial] * 3)
    with pytest.raises(consulta.ConsultaIndisponivel):
        await consulta.datajud_lookup(DIGITS, client)
    assert client.calls == 3
    ok_empty = {"_shards": {"failed": 0}, "hits": {"total": {"value": 0}, "hits": []}}
    assert await consulta.datajud_lookup(DIGITS, FakeClient([ok_empty])) is None


def process_source():
    return {"numeroProcesso": DIGITS, "tribunal": "TRF1", "grau": "G1",
            "classe": {"nome": "Cumprimento de Sentença contra a Fazenda Pública"},
            "orgaoJulgador": {"nome": "14ª Vara Federal Cível da SJDF"}, "assuntos": [{"nome": "Auxílio-Moradia"}],
            "dataAjuizamento": "20240924000000",
            "movimentos": [{"codigo": 848, "nome": "Trânsito em julgado", "dataHora": "20251104120000"},
                           {"codigo": 12457, "nome": "Expedição de precatório/rpv", "dataHora": "20251105160000"}]}


def test_summary_shows_expedition_transit_and_bot_history():
    text = consulta.summarize(DIGITS, process_source(), {"count": 3, "items": [], "requisicao_em": "2025-11-05"},
                              {"nota": 8, "decision": "aprovada"}, 423517.23)
    assert "✅ DataJud: expedição de precatório/RPV em 05/11/2025" in text
    assert "Trânsito em julgado em 04/11/2025" in text and "R$ 423.517,23" in text
    assert "Nota ASPIN 8/10 · aprovada" in text and len(text) < 4000


def test_summary_without_specific_movement_is_cautious():
    src = process_source()
    src["movimentos"] = [m for m in src["movimentos"] if m["codigo"] != 12457]
    text = consulta.summarize(DIGITS, src, None, None)
    assert "sem o movimento específico" in text and "Sem expedição" not in text


def post(text, chat_id=None, chat_type="channel", user_id=7):
    return {"message_id": 99, "text": text, "chat": {"id": chat_id or settings.telegram_chat_id, "type": chat_type},
            "from": {"id": user_id}}


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_id", "-100123")
    worker.COMMAND_TIMES.clear()
    sent = []

    async def send(text, reply_markup=None, html=False, chat_id=None, reply_to=None):
        sent.append({"text": text, "chat_id": chat_id, "reply_to": reply_to})
        return {"message_id": 1, "chat_id": chat_id}
    monkeypatch.setattr(worker, "send_message", send)
    return sent


@pytest.mark.asyncio
async def test_processo_command_replies_in_channel(monkeypatch, chat):
    monkeypatch.setattr(consulta, "datajud_lookup", AsyncMock(return_value=process_source()))
    monkeypatch.setattr(consulta, "djen_lookup", AsyncMock(return_value={"count": 1, "items": [
        {"texto": "Expeça-se ofício requisitório de R$ 423.517,23"}], "requisicao_em": None}))
    monkeypatch.setattr(storage, "process_insight", lambda d: {"opportunity": None, "documents": 2})
    assert await worker.handle_command(post(f"/processo {CNJ}"), set()) is True
    assert "Consultando" in chat[0]["text"]  # aviso imediato
    assert chat[1]["chat_id"] == "-100123" and chat[1]["reply_to"] == 99
    assert "Cumprimento de Sentença" in chat[1]["text"] and "R$ 423.517,23" in chat[1]["text"]


@pytest.mark.asyncio
async def test_invalid_number_and_help(chat):
    await worker.handle_command(post("/processo 123"), set())
    await worker.handle_command(post("/processo 1075810-42.2024.4.01.3400"), set())
    await worker.handle_command(post("/ajuda"), set())
    assert "inválido" in chat[0]["text"] and "inválido" in chat[1]["text"] and "/processo" in chat[2]["text"]


@pytest.mark.asyncio
async def test_outside_channel_only_channel_members_can_query(monkeypatch, chat):
    monkeypatch.setattr(worker, "is_channel_member", AsyncMock(return_value=False))
    await worker.handle_command(post(f"/processo {CNJ}", chat_id="55", chat_type="private"), set())
    assert "restrita" in chat[0]["text"]
    monkeypatch.setattr(worker, "is_channel_member", AsyncMock(return_value=True))
    await worker.handle_command(post("/ajuda", chat_id="-777", chat_type="supergroup"), set())
    assert "/processo" in chat[1]["text"] and chat[1]["chat_id"] == "-777"
    assert await worker.handle_command(post("bom dia"), set()) is False


@pytest.mark.asyncio
async def test_approvers_list_overrides_membership(monkeypatch, chat):
    member = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "is_channel_member", member)
    await worker.handle_command(post("/ajuda", chat_id="55", chat_type="private", user_id=7), {"8"})
    assert "restrita" in chat[0]["text"] and not member.await_count


@pytest.mark.asyncio
async def test_datajud_overload_message_is_not_not_found(monkeypatch, chat):
    monkeypatch.setattr(consulta, "datajud_lookup", AsyncMock(side_effect=consulta.ConsultaIndisponivel("parcial")))
    await worker.handle_command(post(f"/processo {CNJ}"), set())
    assert "sobrecarregada" in chat[1]["text"] and "não significa" in chat[1]["text"]


@pytest.mark.asyncio
async def test_rate_limit(monkeypatch, chat):
    monkeypatch.setattr(settings, "consulta_max_per_minute", 2)
    monkeypatch.setattr(consulta, "datajud_lookup", AsyncMock(return_value=None))
    monkeypatch.setattr(consulta, "djen_lookup", AsyncMock(return_value={"count": 0, "items": []}))
    monkeypatch.setattr(storage, "process_insight", lambda d: {"opportunity": None, "documents": 0})
    for _ in range(3):
        await worker.handle_command(post(f"/processo {CNJ}"), set())
    assert "Muitas consultas" in chat[-1]["text"]


@pytest.mark.asyncio
async def test_commands_flow_through_the_update_loop(monkeypatch, chat):
    handled = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "handle_command", handled)
    state = {}
    monkeypatch.setattr(storage, "get_state", lambda key: state.get(key, {}))
    monkeypatch.setattr(storage, "set_state", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setattr(worker, "sync_sheet", AsyncMock())
    monkeypatch.setattr(worker, "get_updates", AsyncMock(return_value=[{"update_id": 5, "channel_post": post("/ajuda")}]))
    result = await worker.process_buttons()
    assert result["commands"] == 1 and state["telegram_updates"] == {"offset": 6}


def test_datajud_504_is_not_retried():
    from app import datajud
    assert 504 not in datajud.RETRY_STATUSES and 429 in datajud.RETRY_STATUSES
