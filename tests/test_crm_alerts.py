"""Alertas do CRM: follow-up diário, resumo semanal e radar do DJEN nos processos abertos."""
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app import sheets, sources, storage, worker
from app.config import settings

TZ = ZoneInfo("America/Sao_Paulo")
CNJ_A = "0000001-00.2024.4.01.3400"
CNJ_B = "0000002-00.2024.8.26.0100"
# 30/10/2026 é sexta; 02/11/2026 é segunda.
FRIDAY_9H = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)   # 09h em Brasília
FRIDAY_7H = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)   # 07h em Brasília
MONDAY_9H = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def crm_row(**extra):
    row = {"id": "1", "processo": CNJ_A, "linkProcesso": "", "credor": "MARIA APARECIDA DA SILVA",
           "devedor": "União Federal", "tribunal": "TRF1", "tipo": "Precatório", "valor": 300000, "nota": 7,
           "etapa": "Em negociação", "responsavel": "Ana", "proximaAcao": "Ligar para o advogado",
           "dataProximaAcao": "", "etapaAtualizadaEm": "", "comissao": 15000}
    row.update(extra)
    return row


class Store:
    def __init__(self):
        self.state = {}

    def install(self, monkeypatch):
        monkeypatch.setattr(storage, "get_state", lambda key: self.state.get(key, {}))
        monkeypatch.setattr(storage, "set_state", lambda key, value: self.state.__setitem__(key, value))
        return self


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def send(text, reply_markup=None, html=False, chat_id=None, reply_to=None):
        messages.append({"text": text, "html": html, "chat_id": chat_id})
        return {"message_id": len(messages), "chat_id": "-100"}
    monkeypatch.setattr(worker, "send_message", send)
    monkeypatch.setattr(settings, "digest_timezone", "America/Sao_Paulo")
    monkeypatch.setattr(settings, "crm_alert_hour", 8)
    monkeypatch.setattr(settings, "enable_djen", False)
    return messages


def export(rows, url="https://script.google.com/macros/s/x/exec"):
    return {"ok": True, "rows": rows, "stages": list(worker.CRM_STAGES), "appUrl": url}


# ---------- formatação ----------

@pytest.mark.parametrize("name,short", [
    ("MARIA APARECIDA DA SILVA", "Maria S."), ("joão", "João"), ("", "Credor n/i"), (None, "Credor n/i"),
    ("Espólio de José Souza - CPF 123", "Espólio S."),
])
def test_short_name_keeps_privacy(name, short):
    assert worker.short_name(name) == short


def test_followup_groups_by_owner_and_classifies_dates():
    today = date(2026, 10, 30)
    rows = [
        crm_row(credor="Maria Silva", dataProximaAcao="2026-10-28"),
        crm_row(credor="João Souza", dataProximaAcao="2026-10-30", responsavel="Bruno", proximaAcao="Enviar proposta"),
        crm_row(credor="Pedro Lima", dataProximaAcao="2026-11-05"),                       # futura: fora
        crm_row(credor="Ganho Antigo", etapa="Ganho", dataProximaAcao="2026-10-01"),      # fechada: fora
        crm_row(credor="Perdido Antigo", etapa="Perdido", dataProximaAcao="2026-10-01"),  # fechada: fora
        crm_row(credor="Sem Acao Um", proximaAcao="", responsavel=""),
        crm_row(credor="Sem Dono", responsavel="", dataProximaAcao="2026-10-29"),
    ]
    msg = worker.followup_message(rows, today, "https://crm.exemplo/app")
    lines = msg.splitlines()
    assert lines[0] == "<b>ASPIN | Follow-up do CRM — 30/10/2026</b>"
    assert "Atrasadas: 2 — Para hoje: 1 — Sem próxima ação: 1" in msg
    assert "• [ATRASADA] Maria S. — R$ 300.000,00 — Em negociação — Ligar para o advogado — 28/10" in lines
    assert "• [HOJE] João S. — R$ 300.000,00 — Em negociação — Enviar proposta — 30/10" in lines
    assert lines.index("<b>Ana</b>") < lines.index("<b>Bruno</b>") < lines.index("<b>Sem responsável</b>")
    assert "Pedro" not in msg and "Ganho A." not in msg and "Perdido A." not in msg
    assert "Abertas sem próxima ação: 1 (Sem U.)" in msg
    assert lines[-1] == '<a href="https://crm.exemplo/app">Abrir o CRM</a>'


def test_followup_all_clear_is_short():
    msg = worker.followup_message([crm_row(dataProximaAcao="2026-11-10")], date(2026, 10, 30), "")
    assert "Tudo em dia" in msg and len(msg.splitlines()) == 3 and "<a" not in msg


def test_followup_escapes_html_and_has_no_emoji():
    rows = [crm_row(credor="<b>Ana</b> & Cia", responsavel="<i>x</i>", proximaAcao="<script>",
                    dataProximaAcao="2026-10-01")]
    msg = worker.followup_message(rows, date(2026, 10, 30), 'https://x/"><script>')
    assert "<script>" not in msg and "&lt;script&gt;" in msg and "&lt;i&gt;x&lt;/i&gt;" in msg
    assert '"><script>' not in msg
    assert all(ord(ch) < 0x2600 for ch in msg)  # sem emojis


def test_followup_fits_in_one_telegram_message():
    rows = [crm_row(credor=f"Credor {i} Teste", proximaAcao="x" * 80, dataProximaAcao="2026-10-01") for i in range(200)]
    msg = worker.followup_message(rows, date(2026, 10, 30), "https://crm")
    assert len(msg) <= 4096 and "veja no CRM" in msg and msg.endswith("Abrir o CRM</a>")


def test_weekly_summary_funnel_commission_and_results():
    now = datetime(2026, 11, 2, 9, 0, tzinfo=TZ)
    rows = [
        crm_row(etapa="Nova", valor=200000, comissao=10000),
        crm_row(etapa="", valor=100000, comissao=""),  # sem etapa conta como Nova; comissão = 5%
        crm_row(etapa="Em negociação", valor=500000, comissao=25000, dataProximaAcao="2026-10-30"),
        crm_row(etapa="Ganho", valor=400000, etapaAtualizadaEm="31/10/2026 15:20"),
        crm_row(etapa="Ganho", valor=999999, etapaAtualizadaEm="01/10/2026 10:00"),  # fora da semana
        crm_row(etapa="Perdido", valor=150000, etapaAtualizadaEm="29/10/2026 09:00"),
    ]
    msg = worker.weekly_message(rows, worker.CRM_STAGES, now, "https://crm")
    lines = msg.splitlines()
    assert lines[0] == "<b>ASPIN | Resumo semanal do CRM — 26/10 a 02/11/2026</b>"
    assert "• Nova — 2 — R$ 300.000,00" in lines
    assert "• Em negociação — 1 — R$ 500.000,00" in lines
    assert "• Ganho — 2 — R$ 1.399.999,00" in lines and "• Documentação — 0 — R$ 0,00" in lines
    assert "Comissão potencial em aberto: R$ 40.000,00" in lines
    assert "Ganhos na semana: 1 (R$ 400.000,00)" in lines
    assert "Perdas na semana: 1 (R$ 150.000,00)" in lines
    assert "Ações atrasadas: 1" in lines


@pytest.mark.parametrize("text,marks", [
    ("Defiro a habilitação do cessionário", ["cessão", "habilitação"]),
    ("Expeça-se alvará de levantamento", ["pagamento/levantamento"]),
    ("Determino a PENHORA no rosto dos autos", ["penhora"]),
    ("Cancelamento da requisição", ["cancelamento"]),
    ("Expedida requisição de pagamento ao TRF", []),  # requisição de pagamento não é destaque
    ("Intimação para manifestação", []),
])
def test_radar_highlights(text, marks):
    assert worker.radar_highlights(text) == marks


def test_radar_message_content():
    item = {"texto": "<p>Defiro o levantamento</p> " + "x" * 400, "data_disponibilizacao": "2026-10-29",
            "tipoComunicacao": "Intimação", "siglaTribunal": "TRF1"}
    msg = worker.radar_message(crm_row(), item)
    assert "[DESTAQUE] menciona pagamento/levantamento" in msg
    assert "Credor: Maria S." in msg and "MARIA" not in msg
    assert f'<a href="https://comunica.pje.jus.br/consulta?numeroProcesso=00000010020244013400">{CNJ_A}</a>' in msg
    assert "Etapa: Em negociação — Responsável: Ana" in msg
    assert "Comunicação: Intimação — 29/10/2026 (TRF1)" in msg
    trecho = next(line for line in msg.splitlines() if line.startswith("Trecho: "))
    assert len(trecho) <= len("Trecho: ") + 301 and "<p>" not in trecho


# ---------- agendamento e envio ----------

@pytest.mark.asyncio
async def test_daily_followup_sent_once_per_day(monkeypatch, sent):
    store = Store().install(monkeypatch)
    crm = AsyncMock(return_value=export([crm_row(dataProximaAcao="2026-10-29")]))
    monkeypatch.setattr(sheets, "crm_export", crm)
    first = await worker.crm_jobs(FRIDAY_9H)
    assert first["crm_followup"] == "enviado" and len(sent) == 1 and sent[0]["html"]
    assert "[ATRASADA]" in sent[0]["text"] and store.state["crm_followup"]["day"] == "2026-10-30"
    second = await worker.crm_jobs(FRIDAY_9H.replace(hour=20))  # mesmo dia (reinício do robô)
    assert len(sent) == 1 and crm.await_count == 1 and "crm_followup" not in second


@pytest.mark.asyncio
async def test_nothing_before_8h(monkeypatch, sent):
    Store().install(monkeypatch)
    crm = AsyncMock(return_value=export([]))
    monkeypatch.setattr(sheets, "crm_export", crm)
    await worker.crm_jobs(FRIDAY_7H)
    assert sent == [] and crm.await_count == 0


@pytest.mark.asyncio
async def test_old_script_is_skipped_silently(monkeypatch, sent):
    store = Store().install(monkeypatch)
    monkeypatch.setattr(sheets, "crm_export", AsyncMock(return_value=None))
    report = await worker.crm_jobs(FRIDAY_9H)
    assert report["crm"] == "sem_crm_export" and sent == [] and "crm_followup" not in store.state


@pytest.mark.asyncio
async def test_sheet_error_is_recorded_and_retried(monkeypatch, sent):
    store = Store().install(monkeypatch)
    monkeypatch.setattr(sheets, "crm_export", AsyncMock(side_effect=sheets.SheetsError("planilha: HTTP 500")))
    report = await worker.crm_jobs(FRIDAY_9H)
    assert report["crm_error"] == "planilha: HTTP 500" and "crm_followup" not in store.state


@pytest.mark.asyncio
async def test_telegram_failure_does_not_mark_the_day(monkeypatch, sent):
    store = Store().install(monkeypatch)
    monkeypatch.setattr(sheets, "crm_export", AsyncMock(return_value=export([])))
    monkeypatch.setattr(worker, "send_message", AsyncMock(side_effect=worker.TelegramError("Telegram: falha de conexão")))
    report = await worker.crm_jobs(FRIDAY_9H)
    assert report["crm_followup"].startswith("erro: Telegram") and "crm_followup" not in store.state


@pytest.mark.asyncio
async def test_weekly_summary_only_on_monday(monkeypatch, sent):
    store = Store().install(monkeypatch)
    monkeypatch.setattr(sheets, "crm_export", AsyncMock(return_value=export([crm_row()])))
    await worker.crm_jobs(FRIDAY_9H)
    assert "crm_weekly" not in store.state
    report = await worker.crm_jobs(MONDAY_9H)
    assert report["crm_weekly"] == "enviado" and store.state["crm_weekly"]["day"] == "2026-11-02"
    assert any("Resumo semanal" in m["text"] for m in sent)
    before = len(sent)
    await worker.crm_jobs(MONDAY_9H.replace(hour=15))
    assert len(sent) == before


# ---------- radar ----------

def djen_item(i, texto="Intimação das partes", day="2026-10-29"):
    return {"id": i, "hash": f"h{i}", "texto": texto, "data_disponibilizacao": day, "tipoComunicacao": "Intimação"}


def fake_djen(monkeypatch, answers: dict):
    calls = []

    async def search(numero, since, until, client=None, request_count=None, max_pages=3):
        calls.append(numero)
        answer = answers.get(numero)
        if isinstance(answer, sources.DjenProcessResult):
            return answer
        return sources.DjenProcessResult(rows=list(answer or []), errors=[], ok=True)
    monkeypatch.setattr(worker, "search_djen_process", search)
    return calls


A = "00000010020244013400"
B = "00000020020248260100"


@pytest.mark.asyncio
async def test_radar_first_pass_only_registers(monkeypatch, sent):
    store = Store().install(monkeypatch)
    fake_djen(monkeypatch, {A: [djen_item(1), djen_item(2)]})
    report = await worker.radar_pass([crm_row()], FRIDAY_9H)
    assert sent == [] and report["radar_registrados"] == 1
    assert store.state["radar_crm"]["processes"][A]["seen"] == ["h1", "h2"]


@pytest.mark.asyncio
async def test_radar_alerts_only_new_communications(monkeypatch, sent):
    store = Store().install(monkeypatch)
    store.state["radar_crm"] = {"last_run": "2026-10-29T00:00:00+00:00",
                                "processes": {A: {"seen": ["h1"], "checked": "x"}}}
    fake_djen(monkeypatch, {A: [djen_item(1), djen_item(2, "Deferida a penhora")]})
    report = await worker.radar_pass([crm_row()], FRIDAY_9H)
    assert report["radar_alertas"] == 1 and len(sent) == 1
    assert "[DESTAQUE] menciona penhora" in sent[0]["text"]
    assert store.state["radar_crm"]["processes"][A]["seen"] == ["h1", "h2"]
    await worker.radar_pass([crm_row()], FRIDAY_9H)
    assert len(sent) == 1  # já visto


@pytest.mark.asyncio
async def test_radar_skips_closed_processes_and_forgets_them(monkeypatch, sent):
    store = Store().install(monkeypatch)
    store.state["radar_crm"] = {"processes": {A: {"seen": [], "checked": ""}, B: {"seen": [], "checked": ""}}}
    calls = fake_djen(monkeypatch, {A: [djen_item(1)], B: [djen_item(2)]})
    rows = [crm_row(), crm_row(processo=CNJ_B, etapa="Ganho")]
    await worker.radar_pass(rows, FRIDAY_9H)
    assert calls == [A] and list(store.state["radar_crm"]["processes"]) == [A]


@pytest.mark.asyncio
async def test_radar_caps_alerts_per_pass(monkeypatch, sent):
    store = Store().install(monkeypatch)
    monkeypatch.setattr(settings, "radar_max_alerts", 15)
    store.state["radar_crm"] = {"processes": {A: {"seen": [], "checked": ""}}}
    fake_djen(monkeypatch, {A: [djen_item(i) for i in range(20)]})
    report = await worker.radar_pass([crm_row()], FRIDAY_9H)
    assert len(sent) == 15 and report["radar_limitado"] is True
    assert len(store.state["radar_crm"]["processes"][A]["seen"]) == 15
    await worker.radar_pass([crm_row()], FRIDAY_9H)
    assert len(sent) == 20  # o restante sai na passada seguinte


@pytest.mark.asyncio
async def test_radar_failed_lookup_keeps_state_and_403_stops(monkeypatch, sent):
    store = Store().install(monkeypatch)
    store.state["radar_crm"] = {"processes": {A: {"seen": ["h1"], "checked": "1"}, B: {"seen": [], "checked": "2"}}}
    calls = fake_djen(monkeypatch, {
        A: sources.DjenProcessResult(rows=[], errors=["djen_http_403: acesso negado"], blocked=True),
        B: [djen_item(9)]})
    report = await worker.radar_pass([crm_row(), crm_row(processo=CNJ_B)], FRIDAY_9H)
    assert calls == [A] and sent == [] and "403" in report["radar_interrompido"]
    assert store.state["radar_crm"]["processes"][A]["seen"] == ["h1"]
    assert store.state["radar_crm"]["last_run"] == FRIDAY_9H.isoformat()


@pytest.mark.asyncio
async def test_radar_runs_every_6_hours_only_with_djen(monkeypatch, sent):
    store = Store().install(monkeypatch)
    store.state["crm_followup"] = {"day": "2026-10-30"}
    monkeypatch.setattr(sheets, "crm_export", AsyncMock(return_value=export([crm_row()])))
    calls = fake_djen(monkeypatch, {A: []})
    report = await worker.crm_jobs(FRIDAY_9H)
    assert report == {"radar": "djen_desativado"} and calls == []
    monkeypatch.setattr(settings, "enable_djen", True)
    await worker.crm_jobs(FRIDAY_9H)
    assert calls == [A]
    await worker.crm_jobs(FRIDAY_9H.replace(hour=15))  # 3 h depois
    assert calls == [A]
    await worker.crm_jobs(FRIDAY_9H.replace(hour=18, minute=1))  # 6 h depois
    assert calls == [A, A]
