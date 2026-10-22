import argparse
import asyncio
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .config import settings
from .datajud import DataJudError, collect_expeditions
from .llm import LLMClassifier, _AMOUNT_CONTEXT_RE, _normalize_amount
from .opportunity import KEYWORDS, focus_excerpt, max_amount_in_text, canonical_alias, canonical_cnj, clean_text, parse_amount
from .sources import search_djen, probe_tjsp_pages
import html as _html

from . import consulta, sheets
from .telegram_bot import (APPROVE, DISCARD, TelegramError, answer_callback, decided_keyboard,
                           decision_keyboard, get_updates, is_channel_member, send_alert, send_message,
                           set_buttons)
from .source_retry import recover_queries
from . import storage

log = logging.getLogger(__name__)


class CycleFailure(RuntimeError):
    pass


# Códigos de saída: qualquer valor != 0 deixa o workflow vermelho.
EXIT_OK = 0               # success ou partial (inclui "zero oportunidades" com busca concluída)
EXIT_UNEXPECTED = 1       # exceção não prevista
EXIT_CONFIG = 2           # configuração obrigatória ausente
EXIT_SOURCE_FAILED = 3    # busca falhou: DJEN ou DataJud sem nenhuma resposta válida
EXIT_DELIVERY_FAILED = 4  # IA sem nenhuma classificação ou Telegram sem entregar alertas
EXIT_DATABASE = 5         # banco inacessível (causa sanitizada no log)

OUTCOME_EXIT = {"success": EXIT_OK, "partial": EXIT_OK, "skipped": EXIT_OK,
                "source_failed": EXIT_SOURCE_FAILED, "ai_failed": EXIT_DELIVERY_FAILED,
                "alert_failed": EXIT_DELIVERY_FAILED}
SIMULATIONS = ("djen", "datajud", "ia", "telegram", "db")


def simulated(name: str) -> bool:
    """Falha deliberada para validar o caminho de erro (SIMULATE_FAILURE / --simulate-failure)."""
    return name in {x.strip().casefold() for x in str(settings.simulate_failure or "").split(",")}


@contextmanager
def timed(metrics: dict, stage: str):
    started = time.monotonic()
    try:
        yield
    finally:
        metrics.setdefault("duration_s", {})[stage] = round(time.monotonic() - started, 1)


def decide_outcome(metrics: dict, errors: list[str]) -> str:
    """Separa "busca falhou" de "busca concluída sem oportunidades".

    - source_failed: o DJEN não devolveu nenhuma página válida, ou todos os
      tribunais DataJud falharam. Zero oportunidades aqui NÃO é resultado.
    - ai_failed: havia documentos e nenhuma classificação da IA deu certo.
    - alert_failed: havia alertas e nenhum foi entregue ao Telegram.
    - partial: houve falhas isoladas (ficam na fila de retry) ou backlog.
    - success: tudo concluído; opportunities=0 é um resultado válido.
    """
    djen_errors = [e for e in errors if e.startswith(("djen", "ciclo_djen"))]
    if metrics.get("djen_pages", 0) == 0 and djen_errors:
        return "source_failed"
    if metrics.get("tribunals") and metrics.get("datajud_ok", 0) == 0 and metrics.get("datajud_failed"):
        return "source_failed"
    if metrics.get("ai_failed") and not metrics.get("ai_ok"):
        return "ai_failed"
    if metrics.get("alerts_failed") and not metrics.get("alerts"):
        return "alert_failed"
    if errors or metrics.get("limited") or metrics.get("datajud_backlog") or metrics.get("ai_budget_exhausted"):
        return "partial"
    return "success"


def require_configuration():
    missing = settings.configuration_errors()
    if missing:
        raise CycleFailure("Configuração incompleta: " + ", ".join(missing))


def brl(amount) -> str:
    return f"R$ {float(amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# Nota ASPIN: critérios objetivos e verificáveis, somando 10 pontos.
NOTA_CRITERIA = (
    ("valor", "Valor citado no texto", 2),
    ("expedido", "Requisição expedida/ordenada", 2),
    ("datajud", "Expedição confirmada no DataJud", 2),
    ("sem_cessao", "Sem FIDC/banco/cessionário citado", 2),
    ("alimentar", "Natureza alimentar", 1),
    ("recente", "Publicação recente (até 7 dias)", 1),
)


def _published_recently(result: dict, opp: dict, days: int) -> bool:
    """Publicação há até `days` dias (a cliente: quanto mais recente, menos concorrência)."""
    raw = result.get("published_at") or opp.get("created_at")
    if not raw:
        return False
    try:
        when = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw)[:19])
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when <= timedelta(days=days)


def nota_aspin(opp: dict) -> tuple[int, list[tuple[str, str, bool, int]]]:
    """(pontos de 0 a 10, [(chave, descrição, atendido, peso)]).

    Diferente da confiança da IA (opinião do modelo), cada ponto aqui é um fato
    conferível na publicação ou no DataJud.
    """
    result = opp.get("classification") or {}
    checks = {
        "valor": opp.get("amount") is not None and float(opp.get("amount") or 0) > 0,
        "expedido": opp.get("requisition_type") in ("oficio_requisitorio", "precatorio", "rpv"),
        "datajud": bool(opp.get("datajud_signal")),
        "sem_cessao": not result.get("third_party_interest"),
        "alimentar": result.get("credit_nature") == "alimentar",
        "recente": _published_recently(result, opp, days=7),
    }
    items = [(key, label, checks[key], weight) for key, label, weight in NOTA_CRITERIA]
    return sum(weight for _, _, ok, weight in items if ok), items


def rank_score(opp: dict) -> float:
    """Valor esperado do crédito para a ASPIN, usado para escolher as 3 melhores.

    valor × confiança, com ajustes do que o ofício mostra: natureza alimentar e
    preferência legal (pagos antes) sobem; FIDC/banco/cessionário como terceiro
    interessado (crédito possivelmente já cedido) desce bastante.
    """
    result = opp.get("classification") or {}
    score = float(opp.get("amount") or 0) * float(opp.get("potential_score") or 0)
    if result.get("credit_nature") == "alimentar":
        score *= 1.10
    if opp.get("datajud_signal"):
        score *= 1.05
    if result.get("third_party_interest"):
        score *= 0.40
    return score


def _v(value, default="não informado") -> str:
    text = str(value or "").strip()
    return text if text else default


def format_alert(opp: dict, position: int | None = None, total: int | None = None, slot: datetime | None = None) -> str:
    """Alerta no formato do cabeçalho do PJe/ofício requisitório (modelo enviado pela cliente)."""
    result = opp.get("classification") or {}
    signal = opp.get("datajud_signal") or {}
    meta = signal.get("raw_data", {}).get("metadata", {}) if isinstance(signal.get("raw_data"), dict) else {}
    tribunal = str(opp.get("alias") or "não informado").removeprefix("api_publica_").upper()
    url = opp.get("document_url") or opp.get("source_url") or "Link não fornecido pela fonte"
    basis = {"principal": "principal", "bruto": "bruto", "liquido": "líquido"}.get(result.get("amount_basis"), "base não informada")
    tipo = {"oficio_requisitorio": "Ofício requisitório", "precatorio": "Precatório", "rpv": "RPV"}.get(
        opp.get("requisition_type"), _v(opp.get("requisition_type")))
    natureza = {"alimentar": "Alimentar", "comum": "Comum"}.get(result.get("credit_nature"), "não informada")
    header = "ASPIN | Oportunidade"
    if position and total:
        header += f" {position} de {total}"
    if slot:
        header += f" — entrega das {slot:%H}h ({slot:%d/%m})"
    amount = float(opp["amount"])
    lines = [
        header,
        "",
        f"Número: {_v(opp.get('cnj_number'))}",
        f"Classe: {_v(result.get('process_class') or meta.get('classe'))}",
        f"Órgão julgador: {_v(result.get('court_body') or meta.get('orgao'))}",
        f"Tribunal: {tribunal}",
        f"Valor requisitado ({basis}): {brl(amount)}",
        f"Processo referência: {_v(result.get('reference_process'))}",
        f"Assunto: {_v(result.get('subject'))}",
        "",
        f"Requisição: {tipo}" + (f" nº {result['requisition_number']}" if result.get("requisition_number") else ""),
        f"Natureza do crédito: {natureza}",
        f"Preferência legal: {_v(result.get('legal_preference'), 'não informada')}",
        f"Credor: {_v(opp.get('beneficiary'))}",
        f"Devedor: {_v(opp.get('debtor_entity'))}",
    ]
    if result.get("contractual_fees_amount"):
        lines.append(f"Honorários contratuais destacados: {brl(result['contractual_fees_amount'])}")
    if result.get("res_judicata_date"):
        lines.append(f"Trânsito em julgado: {result['res_judicata_date']}")
    lines.append("Terceiro interessado (FIDC/banco/cessionário): " +
                 ("SIM — verificar se o crédito já foi cedido" if result.get("third_party_interest") else "não citado"))
    lines.append(_datajud_line(opp))
    lines += [
        "",
        f"Confiança da classificação: {float(opp['potential_score']):.0%}",
        f"Comissão estimada ASPIN ({settings.aspin_commission_rate:.0%}): {brl(amount * settings.aspin_commission_rate)}",
        "",
        f"Motivo: {str(result.get('reason') or '')[:350]}",
        f"Evidência: {str(opp.get('text_excerpt') or result.get('evidence_quote') or '')[:500]}",
        f"Fonte: {str(url)[:500]}",
        "",
        "Revisão comercial e jurídica necessária: confirmar situação atual, titularidade, cessões e disponibilidade. Comissão é estimativa, não retorno garantido.",
    ]
    return "\n".join(lines)[:4000]


def format_alert_compact(opp: dict, position: int | None = None, total: int | None = None,
                         slot: datetime | None = None) -> str:
    """Notificação enxuta (HTML). Os detalhes completos vão para a planilha ao aprovar."""
    e = _html.escape
    result = opp.get("classification") or {}
    tribunal = str(opp.get("alias") or "").removeprefix("api_publica_").upper() or "Tribunal n/i"
    tipo = {"oficio_requisitorio": "Ofício requisitório", "precatorio": "Precatório", "rpv": "RPV"}.get(
        opp.get("requisition_type"), "Requisição")
    title = "💼 Oportunidade"
    if position and total:
        title += f" {position}/{total}"
    if slot:
        title += f" · {slot:%H}h"
    tags = []
    if result.get("credit_nature") == "alimentar":
        tags.append("Alimentar")
    lines = [f"<b>{e(title)}</b>",
             f"<b>{e(brl(opp['amount']))}</b> · {e(tipo)} · {e(tribunal)}"]
    if tags:
        lines.append(e(" · ".join(tags)))
    lines.append(f"📄 {e(_v(opp.get('cnj_number')))}")
    credor = _v(opp.get("beneficiary"), "credor n/i")
    devedor = _v(opp.get("debtor_entity"), "devedor n/i")
    lines.append(f"👤 {e(credor[:60])} → {e(devedor[:60])}")
    nota, _ = nota_aspin(opp)
    lines.append(f"⭐ <b>Nota ASPIN {nota}/10</b>")
    if result.get("third_party_interest"):
        lines.append("⚠️ FIDC/banco citado — verificar cessão")
    url = opp.get("document_url") or opp.get("source_url")
    if url and str(url).startswith("http"):
        lines.append(f'🔗 <a href="{e(str(url), quote=True)}">Ver publicação</a>')
    return "\n".join(lines)[:4000]


SHEET_HEADERS = (
    "Aprovado em", "Aprovado por", "Nota ASPIN (0-10)", "Valor (R$)", "Credor", "Devedor", "Tribunal", "Tipo",
    "Processo", "Data de expedição (ofício)", "Data base", "Natureza", "Preferência legal", "Terceiro interessado", "Confirmado DataJud",
    "Critérios atendidos", "Confiança IA", "Comissão estimada (R$)", "Honorários destacados (R$)",
    "Nº requisição", "Classe", "Órgão julgador", "Processo referência", "Assunto", "Trânsito em julgado",
    "Fonte", "Publicação (trecho)", "ID interno",
)


def sheet_row(opp: dict) -> dict:
    row = _sheet_values(opp)
    return {header: row.get(header, "") for header in SHEET_HEADERS}


def _sheet_values(opp: dict) -> dict:
    """Linha completa da planilha (cabeçalhos iguais aos da aba)."""
    result = opp.get("classification") or {}
    amount = float(opp.get("amount") or 0)
    decided = opp.get("decided_at")
    decided = decided.astimezone(ZoneInfo(settings.digest_timezone)).strftime("%d/%m/%Y %H:%M") if hasattr(decided, "astimezone") else str(decided or "")
    tipo = {"oficio_requisitorio": "Ofício requisitório", "precatorio": "Precatório", "rpv": "RPV"}.get(
        opp.get("requisition_type"), _v(opp.get("requisition_type"), ""))
    return {
        "Aprovado em": decided,
        "Aprovado por": opp.get("decided_by") or "",
        "Processo": opp.get("cnj_number") or "",
        "Tribunal": str(opp.get("alias") or "").removeprefix("api_publica_").upper(),
        "Tipo": tipo,
        "Nº requisição": result.get("requisition_number") or "",
        "Valor (R$)": round(amount, 2),
        "Natureza": {"alimentar": "Alimentar", "comum": "Comum"}.get(result.get("credit_nature"), ""),
        "Preferência legal": result.get("legal_preference") or "",
        "Data de expedição (ofício)": result.get("issued_at") or "",
        "Data base": result.get("base_date") or "",
        "Credor": opp.get("beneficiary") or "",
        "Devedor": opp.get("debtor_entity") or "",
        "Classe": result.get("process_class") or "",
        "Órgão julgador": result.get("court_body") or "",
        "Processo referência": result.get("reference_process") or "",
        "Assunto": result.get("subject") or "",
        "Honorários destacados (R$)": result.get("contractual_fees_amount") or "",
        "Trânsito em julgado": result.get("res_judicata_date") or "",
        "Terceiro interessado": "Sim — verificar cessão" if result.get("third_party_interest") else "Não citado",
        "Nota ASPIN (0-10)": nota_aspin(opp)[0],
        "Critérios atendidos": "; ".join(label for _, label, ok, _ in nota_aspin(opp)[1] if ok),
        "Confiança IA": round(float(opp.get("potential_score") or 0), 4),
        "Comissão estimada (R$)": round(amount * settings.aspin_commission_rate, 2),
        "Confirmado DataJud": "Sim" if opp.get("datajud_signal") else "Não",
        "Fonte": opp.get("document_url") or opp.get("source_url") or "",
        "Publicação (trecho)": str(opp.get("text_excerpt") or result.get("evidence_quote") or "")[:1500],
        "ID interno": str(opp.get("id") or ""),
    }


def _person(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "sem nome"
    return name + (f" (@{user['username']})" if user.get("username") else "")


COMMAND_TIMES: list[float] = []


async def handle_command(post: dict, approvers: set[str]) -> bool:
    """Responde /processo e /ajuda no canal da ASPIN ou no privado de quem pode aprovar."""
    text = str(post.get("text") or "").strip()
    match = consulta.COMMAND_RE.match(text)
    if not match:
        return False
    chat = post.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    user_id = str((post.get("from") or {}).get("id", ""))
    reply_to = post.get("message_id")
    log.info("Comando recebido (%s, tipo=%s)", match.group(1), chat.get("type"))
    # No próprio canal vale sempre; em privado ou grupo, só para quem é membro do canal
    # (ou está em TELEGRAM_APPROVERS).
    if chat_id != str(settings.telegram_chat_id):
        allowed = user_id in approvers if approvers else await is_channel_member(user_id)
        if not allowed:
            await send_message("Consulta restrita à equipe da ASPIN (membros do canal de oportunidades).",
                               chat_id=chat_id, reply_to=reply_to)
            return True
    command, arg = match.group(1).casefold(), match.group(2)
    if command in ("ajuda", "start") or not consulta.only_digits(arg):
        await send_message(consulta.HELP_TEXT, html=True, chat_id=chat_id, reply_to=reply_to)
        return True
    now = time.monotonic()
    COMMAND_TIMES[:] = [t for t in COMMAND_TIMES if now - t < 60]
    if len(COMMAND_TIMES) >= settings.consulta_max_per_minute:
        await send_message("Muitas consultas no último minuto; tente de novo em instantes.", chat_id=chat_id, reply_to=reply_to)
        return True
    COMMAND_TIMES.append(now)
    digits = consulta.only_digits(arg)
    if len(digits) != 20 or not consulta.cnj_check_digits_ok(digits):
        await send_message("Número CNJ inválido. Use o formato 0000000-00.0000.0.00.0000.", chat_id=chat_id, reply_to=reply_to)
        return True
    if not consulta.alias_from_cnj(digits):
        await send_message("Esse tribunal não tem índice público no DataJud (ex.: STF).", chat_id=chat_id, reply_to=reply_to)
        return True
    # Aviso imediato: a API do CNJ costuma levar de 10 s a 1 min.
    await send_message(f"🔎 Consultando {consulta.format_cnj(digits)} no CNJ… pode levar até 1 minuto.",
                       chat_id=chat_id, reply_to=reply_to)
    try:
        process = await consulta.datajud_lookup(digits)
    except (consulta.ConsultaIndisponivel, DataJudError):
        await send_message("A API do CNJ (DataJud) está sobrecarregada agora; tente novamente em alguns minutos. "
                           "Isso não significa que o processo não existe.", chat_id=chat_id, reply_to=reply_to)
        return True
    djen = None
    max_amount = None
    try:
        djen = await consulta.djen_lookup(digits)
        amounts = [max_amount_in_text(clean_text(i.get("texto") or "")) for i in djen.get("items", [])]
        amounts = [a for a in amounts if a]
        max_amount = max(amounts) if amounts else None
    except Exception:
        djen = {"error": "DJEN indisponível no momento"}
    internal = None
    try:
        insight = storage.process_insight(digits)
        opp = insight.get("opportunity")
        internal = {"documents": insight.get("documents", 0)}
        if opp:
            opp = dict(opp)
            opp["datajud_signal"] = storage.datajud_signal(opp.get("cnj_number"))
            internal.update(nota=nota_aspin(opp)[0], decision=opp.get("decision"))
    except Exception:
        internal = None
    await send_message(consulta.summarize(digits, process, djen, internal, max_amount), html=True,
                       chat_id=chat_id, reply_to=reply_to)
    return True


async def process_buttons(poll_seconds: int = 0) -> dict:
    """Lê os cliques em Aprovar/Descartar, grava a decisão e envia as aprovadas à planilha."""
    metrics = {"clicks": 0, "approved": 0, "discarded": 0, "ignored": 0, "sheet_ok": 0, "sheet_failed": 0}
    approvers = {x.strip() for x in settings.telegram_approvers.split(",") if x.strip()}
    state = storage.get_state("telegram_updates")
    offset = state.get("offset")
    updates = await get_updates(offset, poll_seconds) if poll_seconds else await get_updates(offset)
    for update in updates:
        offset = int(update["update_id"]) + 1
        post = update.get("message") or update.get("channel_post")
        if post:
            try:
                if await handle_command(post, approvers):
                    metrics["commands"] = metrics.get("commands", 0) + 1
            except Exception as exc:
                log.warning("Comando não respondido: %s", storage.safe_error(exc))
            continue
        query = update.get("callback_query") or {}
        if not query:
            continue
        metrics["clicks"] += 1
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        data = str(query.get("data") or "")
        user = query.get("from") or {}
        if chat_id != str(settings.telegram_chat_id) or ":" not in data:
            metrics["ignored"] += 1
            await answer_callback(query.get("id", ""), "Já decidido.")
            continue
        if approvers and str(user.get("id")) not in approvers:
            metrics["ignored"] += 1
            await answer_callback(query.get("id", ""), "Você não está autorizado a decidir oportunidades.")
            continue
        action, opp_id = data.split(":", 1)
        if action not in (APPROVE, DISCARD):
            metrics["ignored"] += 1
            continue
        decision = "aprovada" if action == APPROVE else "descartada"
        who = _person(user)
        try:
            created, current = storage.record_decision(opp_id, decision, who)
        except Exception as exc:
            log.error("Decisão não gravada: %s", storage.safe_error(exc))
            offset = int(update["update_id"])  # o clique será reprocessado no próximo ciclo
            metrics["decision_db_error"] = storage.safe_error(exc)
            break
        if current is None:
            metrics["ignored"] += 1
            await answer_callback(query.get("id", ""), "⌛ Expirou: sem resposta em 48 h, a oportunidade foi removida.")
            try:
                await set_buttons(chat_id, message.get("message_id"), decided_keyboard("⌛ Expirou (48h)"))
            except TelegramError:
                pass
            continue
        final = current.get("decision")
        by = current.get("decided_by") or who
        label = ("✅ Aprovado por " if final == "aprovada" else "❌ Descartado por ") + by.split(" (")[0]
        await answer_callback(query.get("id", ""), label if created else f"Já decidido: {label}")
        try:
            await set_buttons(chat_id, message.get("message_id"), decided_keyboard(label))
        except TelegramError as exc:
            log.warning("Botões não atualizados: %s", exc)
        if created:
            metrics["approved" if decision == "aprovada" else "discarded"] += 1
    if updates:
        storage.set_state("telegram_updates", {"offset": offset})
    await sync_sheet(metrics)
    return metrics


LAST_FORMAT_TRY = [float("-inf")]


async def sync_sheet(metrics: dict) -> None:
    """Fila de saída: toda aprovação ainda não gravada na planilha é (re)enviada."""
    if not sheets.configured():
        pending = storage.sheet_outbox(1000)
        metrics["sheet_pending"] = len(pending)
        if pending:
            metrics["sheet_not_configured"] = True
        return
    # Reorganiza/formata a planilha uma vez por versão de layout (após atualizar o script).
    # No robô sempre ligado, tenta no máximo a cada 10 min enquanto o script novo não estiver publicado.
    if (storage.get_state("sheet_layout").get("version") != sheets.LAYOUT_VERSION
            and time.monotonic() - LAST_FORMAT_TRY[0] >= 600):
        LAST_FORMAT_TRY[0] = time.monotonic()
        try:
            await sheets.format_sheet()
            storage.set_state("sheet_layout", {"version": sheets.LAYOUT_VERSION})
            metrics["sheet_formatted"] = True
        except sheets.SheetsError as exc:
            metrics["sheet_format_pending"] = str(exc)
    for opp in storage.sheet_outbox(50):
        try:
            opp["datajud_signal"] = storage.datajud_signal(opp.get("cnj_number"))
        except Exception:
            opp["datajud_signal"] = None
        try:
            await sheets.append_row(sheet_row(opp))
            storage.mark_sheet(opp["id"])
            metrics["sheet_ok"] += 1
        except sheets.SheetsError as exc:
            storage.mark_sheet(opp["id"], str(exc))
            metrics["sheet_failed"] += 1
            metrics["sheet_error"] = str(exc)
            break


def _datajud_line(opp: dict) -> str:
    signal = opp.get("datajud_signal")
    if not signal:
        return "DataJud: expedição de precatório/RPV ainda não indexada para este processo"
    when = signal.get("published_at")
    when = when.date().isoformat() if hasattr(when, "date") else str(when or "data não informada")[:10]
    return f"DataJud: expedição de precatório/RPV registrada ({when})"


def status_message(metrics: dict, errors: list[str]) -> str:
    queue = metrics.get("queue", {})
    pending = sum(queue.get(k, 0) for k in ("pending", "retry", "processing"))
    outcome = metrics.get("outcome")
    if outcome in ("source_failed", "ai_failed", "alert_failed"):
        title = f"ASPIN | FALHA ({outcome}) — não é confirmação de ausência de oportunidades"
    elif errors:
        title = "ASPIN | Coleta com falhas — não é confirmação de ausência de oportunidades"
    elif metrics.get("limited") or pending:
        title = "ASPIN | Coleta parcial — há cobertura ou processamento pendente"
    else:
        title = "ASPIN | Resumo da busca concluída nesta janela"
    text = (
        f"{title}\n\n"
        f"Janela: {metrics.get('since', 'não informada')}\n"
        f"Publicações DJEN capturadas: {metrics.get('djen_documents', 0)}\n"
        f"Páginas DJEN consultadas: {metrics.get('djen_pages', 0)}\n"
        f"Sinais DataJud (expedição precatório/RPV): {metrics.get('datajud_documents', 0)} em {metrics.get('datajud_ok', 0)}/{metrics.get('tribunals', 0)} tribunais\n"
        f"Classificações neste ciclo: {metrics.get('classified', 0)}\n"
        f"Candidatos novos com valor mínimo: {metrics.get('opportunities', 0)}\n"
        f"Alertas enviados: {metrics.get('alerts', 0)}\n"
        f"Fila pendente: {pending}\n"
        f"Aguardando enriquecimento: {queue.get('needs_enrichment', 0)}\n"
        f"Falhas aguardando revisão: {queue.get('failed', 0)}"
    )
    if metrics.get("djen_disabled"):
        text += "\nDJEN: desativado neste coletor (a API do DJEN só aceita IPs do Brasil)"
    if errors:
        text += "\n\nFalhas: " + "; ".join(errors[:4])[:800]
    elif not metrics.get("opportunities"):
        text += "\n\nNenhum candidato novo aprovado nesta execução; isso não significa ausência de créditos no mercado."
    return text


async def maybe_send_status(metrics: dict, errors: list[str]):
    if settings.status_telegram != "always" and metrics.get("outcome") not in ("source_failed", "ai_failed", "alert_failed"):
        return  # canal recebe só as melhores oportunidades; status apenas em falha
    now = datetime.now(timezone.utc)
    last = storage.get_state("last_status")
    try:
        stamp = datetime.fromisoformat(last.get("at", ""))
        due = (now - stamp).total_seconds() >= settings.status_interval_minutes * 60
    except (TypeError, ValueError):
        due = True
    if due:
        await send_alert(status_message(metrics, errors))
        storage.set_state("last_status", {"at": now.isoformat()})


async def _collect_datajud(metrics: dict, errors: list[str]):
    """Sinais de expedição de precatório/RPV no DataJud, com cursor por tribunal."""
    if not settings.enable_datajud:
        metrics["datajud_disabled"] = True
        return
    aliases = [canonical_alias(x) for x in settings.datajud_tribunals.split(",") if x.strip()]
    # Rodízio: cada ciclo começa num tribunal diferente, para o orçamento de tempo
    # não deixar sempre os últimos da lista sem consulta.
    start = int(storage.get_state("datajud_rotation").get("next", 0)) % max(1, len(aliases))
    aliases = aliases[start:] + aliases[:start]
    storage.set_state("datajud_rotation", {"next": start + 1})
    deadline = time.monotonic() + settings.datajud_time_budget_seconds
    for alias in aliases:
        if time.monotonic() >= deadline:
            metrics.setdefault("datajud_not_reached", []).append(alias)  # fica para o próximo ciclo
            metrics["datajud_backlog"] = True
            continue
        if metrics["tribunals"]:
            await asyncio.sleep(settings.datajud_request_delay)  # espaça tribunais (limite 429 do CNJ)
        metrics["tribunals"] += 1
        key = "datajud_cursor:" + alias
        try:
            if simulated("datajud"):
                raise DataJudError("falha simulada")
            rows, cursor, info = await collect_expeditions(alias, storage.get_state(key))
            storage.upsert_documents(rows)
            storage.set_state(key, cursor)  # só avança depois de gravar tudo
            metrics["datajud_documents"] += info["documents"]
            metrics["datajud_pages"] += info["pages"]
            metrics["datajud_ok"] += 1
            if not info["exhausted"]:
                metrics["datajud_backlog"] = True
        except Exception as exc:
            reason = str(exc) if isinstance(exc, DataJudError) else storage.safe_error(exc)
            metrics["datajud_failed"].append(alias)
            errors.append(f"datajud {alias}: {reason}")
            log.error("DataJud %s indisponível: %s", alias, reason)


async def _process_queue(metrics: dict, errors: list[str]):
    classifier = LLMClassifier()
    # Nunca passa do prazo do ciclo: reserva tempo para gravar e entregar no Telegram.
    deadline = min(time.monotonic() + settings.ai_time_budget_seconds, CYCLE_DEADLINE[0] - settings.cycle_reserve_seconds)
    ai_calls = 0
    claims = 0
    while ai_calls < settings.max_classify_per_cycle and claims < settings.max_claims_per_cycle:
        claims += 1
        if time.monotonic() >= deadline:
            # Orçamento de tempo esgotado: o restante segue na fila para o próximo ciclo.
            metrics["ai_budget_exhausted"] = True
            break
        doc = storage.claim_document()
        if not doc:
            break
        try:
            raw = doc.get("raw_data") or {}
            if raw.get("motivo_cancelamento") or raw.get("motivoCancelamento"):
                storage.finish_document(doc, "cancelled")
                continue
            text = clean_text(doc.get("text_excerpt") or "")
            # Pré-filtro também para itens antigos da fila (gravados antes dele existir):
            # sem valor citado ou abaixo do mínimo não gasta IA nem conta nas 25 do ciclo.
            amount_hint = max_amount_in_text(text)
            triage = storage.initial_status(doc.get("source") or "djen", text, amount_hint)
            if triage != "pending":
                storage.finish_document(doc, triage, {"rule": "pre_filtro_local", "max_amount": amount_hint})
                metrics["prefiltered"] = metrics.get("prefiltered", 0) + 1
                if triage == "below_minimum":
                    metrics["below_minimum"] += 1
                continue
            amounts = [_normalize_amount(match.group(1)) for match in _AMOUNT_CONTEXT_RE.finditer(text)]
            amounts = [amount for amount in amounts if amount is not None and amount >= 0]
            if amounts and max(amounts) < settings.min_opportunity_amount:
                storage.finish_document(doc, "below_minimum", {"rule": "todos_valores_explicitamente_abaixo_do_minimo", "max_amount": max(amounts)})
                metrics["below_minimum"] += 1
                continue
            # Nunca marca como concluído antes da resposta da IA e persistência.
            if simulated("ia"):
                result = {"error": True, "reason": "falha simulada da IA"}
            else:
                remaining = deadline - time.monotonic()
                try:
                    result = await asyncio.wait_for(classifier.classify(focus_excerpt(text, settings.ai_max_chars)), timeout=max(1.0, remaining))
                except (TimeoutError, asyncio.TimeoutError):
                    # Prazo do ciclo: o documento volta para a fila sem contar como falha da IA.
                    storage.finish_document(doc, "retry", error="prazo_do_ciclo")
                    metrics["ai_budget_exhausted"] = True
                    break
            ai_calls += 1
            metrics["classified"] += 1
            if isinstance(result, dict) and doc.get("published_at"):
                result.setdefault("published_at", str(doc.get("published_at")))
            if result.get("error"):
                metrics["ai_failed"] = metrics.get("ai_failed", 0) + 1
                reason = str(result.get("reason") or "provedores indisponíveis")[:240]
                storage.finish_document(doc, "retry", error=reason)
                errors.append("ia: " + reason)
                # Circuit breaker: não esgota todos os documentos se provedores estão indisponíveis.
                if "resposta" in reason or "ValueError" in reason:
                    continue
                break
            metrics["ai_ok"] = metrics.get("ai_ok", 0) + 1
            if not result.get("is_opportunity"):
                status = "needs_enrichment" if result.get("needs_enrichment") or result.get("need_enrichment") else "rejected"
                storage.finish_document(doc, status, result)
                metrics[status] = metrics.get(status, 0) + 1
                continue
            amount = parse_amount(result.get("amount"))
            if amount is None:
                storage.finish_document(doc, "needs_enrichment", result)
                continue
            if amount < settings.min_opportunity_amount:
                storage.finish_document(doc, "below_minimum", result)
                metrics["below_minimum"] += 1
                continue
            result["amount"] = amount
            _, created = storage.save_opportunity(doc, result)
            storage.finish_document(doc, "classified", result)
            metrics["opportunities"] += int(created)
        except asyncio.CancelledError:
            storage.finish_document(doc, "retry", error="ciclo_interrompido")
            raise
        except Exception as exc:
            error = storage.safe_error(exc)
            storage.finish_document(doc, "retry", error=error)
            errors.append("processamento: " + error)
            log.error("Documento pendente para nova tentativa: %s", error)


FAILURE_OUTCOMES = ("source_failed", "ai_failed", "alert_failed")


def apply_failure_streak(outcome: str, metrics: dict) -> str:
    """Falha passageira (fonte fora, IA em limite, Telegram instável) vira partial e
    é refeita no próximo ciclo; só fica vermelho quando a mesma falha se repete em
    FAILURE_STREAK_TO_ALERT ciclos seguidos, ou numa simulação deliberada."""
    state = storage.get_state("failure_streak")
    if outcome not in FAILURE_OUTCOMES:
        if state:
            storage.set_state("failure_streak", {})
        return outcome
    if settings.simulate_failure:
        return outcome
    streak = int(state.get(outcome, 0)) + 1
    storage.set_state("failure_streak", {outcome: streak})
    metrics["failure_streak"] = streak
    if streak < settings.failure_streak_to_alert:
        metrics["falha_transitoria"] = outcome
        return "partial"
    return outcome


def digest_slot_due(now: datetime) -> datetime | None:
    """Horário de entrega (08/12/18h locais) mais recente ainda dentro da tolerância de atraso."""
    tz = ZoneInfo(settings.digest_timezone)
    local = now.astimezone(tz)
    hours = sorted({int(h) for h in settings.digest_hours.split(",") if h.strip()})
    candidates = [datetime.combine(local.date() - timedelta(days=d), datetime.min.time(), tz).replace(hour=h)
                  for d in (0, 1) for h in hours]
    past = [slot for slot in candidates if slot <= local]
    if not past:
        return None
    slot = max(past)
    if (local - slot).total_seconds() > settings.digest_max_delay_minutes * 60:
        return None
    return slot


def digest_message_empty(slot: datetime, metrics: dict) -> str:
    return (f"ASPIN | Entrega das {slot:%H:%M} ({slot:%d/%m})\n\n"
            "Nenhuma oportunidade passou nos critérios nesta janela "
            f"(confiança ≥ {settings.min_confidence:.0%} e valor ≥ {brl(settings.min_opportunity_amount)}).\n"
            "Isto não significa ausência de créditos no mercado; a coleta continua a cada hora.")


async def _deliver_digest(metrics: dict, errors: list[str], now: datetime | None = None):
    """Envia só as N melhores da janela, nos horários configurados.

    Se o envio falhar, o horário não é dado como entregue e o próximo ciclo
    (de hora em hora) tenta de novo dentro da tolerância; nada é enviado duas
    vezes porque cada oportunidade enviada recebe alerted_at.
    """
    now = now or datetime.now(timezone.utc)
    slot = digest_slot_due(now)
    if slot is None:
        metrics["digest"] = "fora_do_horario"
        return
    key = slot.isoformat()
    if storage.get_state("last_digest").get("slot") == key:
        metrics["digest"] = "ja_entregue"
        return
    # Progresso do horário: se o envio caiu no meio, a nova tentativa manda só o que falta.
    progress = storage.get_state("digest_progress")
    sent_ids = list(progress.get("sent_ids", [])) if progress.get("slot") == key else []
    remaining = max(0, settings.digest_top_n - len(sent_ids))
    best = []
    if remaining:
        # Avalia uma amostra ampla (já ordenada por valor × confiança no banco) e
        # reordena com os campos do ofício: natureza, preferência, cessão.
        pool = storage.pending_alerts(settings.digest_pool_size)
        for opp in pool:
            try:
                opp["datajud_signal"] = storage.datajud_signal(opp.get("cnj_number"))
            except Exception:
                opp["datajud_signal"] = None
        best = sorted(pool, key=rank_score, reverse=True)[:remaining]
    metrics["digest_slot"] = slot.isoformat()
    if not best and not sent_ids:
        if simulated("telegram"):
            raise RuntimeError("Telegram: falha simulada")
        await send_alert(digest_message_empty(slot, metrics))
    elif best:
        before = metrics.get("alerts", 0)
        await _send_pending_alerts(metrics, errors, best, slot, len(sent_ids))
        sent_ids += [str(o["id"]) for o in best[:metrics.get("alerts", 0) - before]]
        if metrics.get("alerts_failed"):
            storage.set_state("digest_progress", {"slot": key, "sent_ids": sent_ids})
            metrics["digest"] = "envio_pendente"
            return
    archived = storage.archive_unselected(sent_ids, now)
    storage.set_state("last_digest", {"slot": key, "sent": len(sent_ids), "archived": archived, "at": now.isoformat()})
    storage.set_state("digest_progress", {})
    metrics["digest"] = "entregue"
    metrics["digest_sent"] = len(sent_ids)
    metrics["digest_archived"] = archived


async def _send_pending_alerts(metrics: dict, errors: list[str], opportunities: list[dict] | None = None,
                               slot: datetime | None = None, already_sent: int = 0):
    if opportunities is None:
        opportunities = storage.pending_alerts(settings.max_classify_per_cycle)
    for index, opp in enumerate(opportunities, start=already_sent + 1):
        try:
            if simulated("telegram"):
                raise RuntimeError("Telegram: falha simulada")
            if "datajud_signal" not in opp:
                try:
                    opp["datajud_signal"] = storage.datajud_signal(opp.get("cnj_number"))
                except Exception:
                    opp["datajud_signal"] = None
            total = settings.digest_top_n if slot else None
            sent = await send_message(format_alert_compact(opp, index if slot else None, total, slot),
                                      decision_keyboard(opp["id"]), html=True)
            storage.mark_alerted(opp["id"])
            try:
                storage.set_telegram_message(opp["id"], sent["chat_id"], sent["message_id"])
            except Exception as exc:
                log.warning("message_id não gravado: %s", storage.safe_error(exc))
            metrics["alerts"] += 1
        except Exception as exc:
            reason = str(exc) if str(exc).startswith("Telegram") else storage.safe_error(exc)
            errors.append("telegram: " + reason)
            metrics["alerts_failed"] = metrics.get("alerts_failed", 0) + 1
            log.error("Alerta continua pendente: %s", reason)
            break


CYCLE_DEADLINE = [float("inf")]


async def run_cycle(lookback_minutes: int | None = None) -> dict:
    CYCLE_DEADLINE[0] = time.monotonic() + settings.cycle_timeout_seconds
    require_configuration()
    if simulated("db"):
        raise storage.DatabaseUnavailable("Banco: causa=falha_simulada | como corrigir: remova SIMULATE_FAILURE")
    storage.ensure_schema()  # idempotente; cria também a tabela do lease
    with storage.collector_lock() as locked:
        if not locked:
            holder = storage.lease_holder()
            log.warning("Outro coletor já está executando; ciclo não duplicado (%s)", holder)
            return {"outcome": "skipped", "opportunities": 0, "errors": [], "lock_holder": holder}
        now = datetime.now(timezone.utc)
        last = storage.get_state("last_collection")
        if lookback_minutes is None:
            since = now - timedelta(days=settings.initial_lookback_days)
            if last.get("at"):
                try:
                    since = max(since, datetime.fromisoformat(last["at"]) - timedelta(minutes=settings.incremental_lookback_minutes))
                except ValueError:
                    pass
        else:
            since = now - timedelta(minutes=max(1, lookback_minutes))
        metrics = {"since": since.isoformat(), "djen_documents": 0, "djen_pages": 0,
                   "datajud_documents": 0, "datajud_pages": 0, "datajud_ok": 0, "datajud_failed": [],
                   "tribunals": 0, "classified": 0, "ai_ok": 0, "ai_failed": 0,
                   "opportunities": 0, "alerts": 0, "alerts_failed": 0, "below_minimum": 0, "limited": False}
        if settings.simulate_failure:
            metrics["simulated_failure"] = settings.simulate_failure
        if storage.DSN_REPAIRED[0]:
            metrics["aviso_config"] = "DATABASE_URL com região do pooler sem hífen; corrigida em memória — corrija o secret"
        errors = []
        sources = (["djen"] if settings.enable_djen else []) + (["datajud"] if settings.enable_datajud else [])
        run_id = storage.start_run(sources, "simulation" if settings.simulate_failure else "scheduled")
        try:
            cursor = storage.get_state("djen_cursor") or None
            query_since = date.fromisoformat(cursor["since"]) if cursor else since.date()
            query_until = date.fromisoformat(cursor["until"]) if cursor else now.date()
            with timed(metrics, "djen"):
                if not settings.enable_djen:
                    # Ex.: GitHub Actions (IP fora do Brasil): DJEN roda em outro coletor.
                    metrics["djen_disabled"] = True
                    batch = SimpleNamespace(rows=[], pages=0, totals={}, limited=False, cursor=cursor,
                                            errors=[], retry_cursors=[])
                elif simulated("djen"):
                    batch = SimpleNamespace(rows=[], pages=0, totals={}, limited=False, cursor=cursor,
                                            errors=["djen_simulado: falha simulada da fonte"], retry_cursors=[])
                else:
                    batch = await search_djen(query_since, query_until, cursor=cursor)
                metrics["djen_documents"] = len(batch.rows)
                metrics["djen_pages"] = batch.pages
                metrics["djen_totals"] = batch.totals
                metrics["limited"] = batch.limited
                errors.extend(batch.errors)
                storage.upsert_documents(batch.rows)
                if settings.enable_djen and not simulated("djen"):
                    storage.set_state("djen_cursor", batch.cursor or {})
                    await recover_queries(batch, metrics, errors, now)
            with timed(metrics, "datajud"):
                await _collect_datajud(metrics, errors)
            if settings.enable_tjsp_probe:
                # Monitoramento explicitamente separado da descoberta (sem contornar captcha).
                probes = await probe_tjsp_pages()
                metrics["tjsp_probe_available"] = len(probes)
            with timed(metrics, "ia"):
                try:
                    triaged = storage.backfill_prefilter(time_budget=settings.backfill_time_budget_seconds)
                    if triaged:
                        metrics["prefilter_backfill"] = triaged
                except Exception as exc:
                    log.warning("Triagem em lote adiada: %s", storage.safe_error(exc))
                await _process_queue(metrics, errors)
            with timed(metrics, "telegram"):
                try:
                    await _deliver_digest(metrics, errors)
                except Exception as exc:
                    reason = str(exc) if str(exc).startswith("Telegram") else storage.safe_error(exc)
                    errors.append("telegram: " + reason)
                    metrics["alerts_failed"] = metrics.get("alerts_failed", 0) + 1
            metrics["queue"] = storage.queue_counts()
            outcome = apply_failure_streak(decide_outcome(metrics, errors), metrics)
            metrics["outcome"] = outcome
            try:
                await maybe_send_status(metrics, errors)
            except Exception as exc:
                errors.append("status_telegram: " + storage.safe_error(exc))
            if settings.enable_djen and outcome != "source_failed" and batch.cursor is None:
                covered = min(now, datetime.combine(query_until + timedelta(days=1), datetime.min.time(), timezone.utc))
                storage.set_state("last_collection", {"at": covered.isoformat()})
            storage.set_state("last_health", {"at": datetime.now(timezone.utc).isoformat(), "outcome": outcome})
            storage.finish_run(run_id, outcome, metrics, errors)
            log.info("Ciclo concluído: %s", json.dumps({"outcome": outcome, **metrics, "errors": errors}, ensure_ascii=False, default=str))
            return {"outcome": outcome, **metrics, "errors": errors}
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                error = "timeout_ou_interrupcao"
            else:
                error = storage.safe_error(exc)
            errors.append("ciclo: " + error)
            try:
                storage.finish_run(run_id, "failed", metrics, errors)
            except Exception:
                log.error("Não foi possível registrar a falha do ciclo no banco")
            raise


async def poll_once(lookback_minutes: int | None = None) -> int:
    result = await run_cycle(lookback_minutes)
    if OUTCOME_EXIT.get(result["outcome"], EXIT_UNEXPECTED) != EXIT_OK:
        raise CycleFailure("Ciclo com falhas: " + "; ".join(result["errors"][:5]))
    return result["opportunities"]


async def run_forever():
    require_configuration()
    while True:
        started = asyncio.get_running_loop().time()
        interval = settings.poll_interval_minutes
        try:
            result = await asyncio.wait_for(run_cycle(), timeout=settings.cycle_timeout_seconds)
            if result["outcome"] not in ("success", "skipped"):
                interval = min(interval, 5)
        except Exception as exc:
            log.error("Ciclo não concluído: %s", storage.safe_error(exc))
            interval = min(interval, 5)
        delay = interval * 60 - (asyncio.get_running_loop().time() - started)
        await asyncio.sleep(max(0, delay))


EXAMPLE_OPPORTUNITY = {
    "id": "exemplo", "amount": 338813.79, "potential_score": 0.92, "alias": "api_publica_trf1",
    "requisition_type": "precatorio", "cnj_number": "0000000-00.2024.4.01.3400",
    "beneficiary": "CREDOR FICTÍCIO", "debtor_entity": "União Federal",
    "document_url": "(exemplo — sem link)",
    "text_excerpt": "Requisito o pagamento em favor do(s) credor(es) ... Valor Total do Beneficiário: R$ 338.813,79",
    "classification": {
        "amount_basis": "principal", "process_class": "CUMPRIMENTO DE SENTENÇA CONTRA A FAZENDA PÚBLICA",
        "court_body": "14ª Vara Federal Cível da SJDF", "reference_process": "0000000-00.2014.4.01.3400",
        "subject": "Descontos indevidos", "requisition_number": "2025.0000.000.000000",
        "credit_nature": "alimentar", "legal_preference": "Idosos a partir de 60 anos",
        "contractual_fees_amount": 84703.44, "res_judicata_date": "25/04/2024", "third_party_interest": False,
        "reason": "Requisição de pagamento expedida com valor do beneficiário, natureza alimentar e preferência de idoso.",
    },
}


async def send_preview() -> int:
    """Prévia da entrega das 12h no canal: as 3 melhores atuais, sem marcar como enviadas.

    Se ainda não houver 3 oportunidades qualificadas, completa com um exemplo
    claramente marcado como fictício.
    """
    tz = ZoneInfo(settings.digest_timezone)
    slot = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
    try:
        pool = storage.pending_alerts(settings.digest_pool_size)
        for opp in pool:
            opp["datajud_signal"] = storage.datajud_signal(opp.get("cnj_number"))
        best = sorted(pool, key=rank_score, reverse=True)[:settings.digest_top_n]
    except Exception as exc:
        log.warning("Prévia sem dados do banco: %s", storage.safe_error(exc))
        best = []
    real = len(best)
    await send_alert(
        "ASPIN | PRÉVIA da entrega das 12h — não conta como entrega\n\n"
        f"Assim chegam as {settings.digest_top_n} melhores oportunidades às 08h, 12h e 18h. "
        f"Oportunidades reais qualificadas agora: {real}."
        + ("" if real >= settings.digest_top_n else
           f" As demais desta prévia são EXEMPLO FICTÍCIO baseado no ofício modelo."))
    total = settings.digest_top_n
    for index in range(1, total + 1):
        if index <= real:
            opp = best[index - 1]
            text = "🔎 <i>PRÉVIA</i>\n" + format_alert_compact(opp, index, total, slot)
            sent = await send_message(text, decision_keyboard(opp["id"]), html=True)
            try:
                storage.set_telegram_message(opp["id"], sent["chat_id"], sent["message_id"])
            except Exception as exc:
                log.warning("message_id não gravado: %s", storage.safe_error(exc))
        else:
            text = "🧪 <i>EXEMPLO FICTÍCIO — sem botões</i>\n" + format_alert_compact(dict(EXAMPLE_OPPORTUNITY), index, total, slot)
            await send_message(text, html=True)
    print(f"Prévia enviada: {real} reais + {total - min(real, total)} exemplo(s)")
    return EXIT_OK


def collector_dispatch_reason(now: datetime | None = None) -> str | None:
    """Vigia: por que disparar a coleta agora (o cron do GitHub atrasa ou pula execuções)."""
    now = now or datetime.now(timezone.utc)
    if storage.lease_holder():
        return None  # já há um ciclo rodando
    slot = digest_slot_due(now)
    if slot is not None and storage.get_state("last_digest").get("slot") != slot.isoformat():
        return f"entrega das {slot:%H}h pendente"
    minutes = storage.minutes_since_last_cycle()
    if minutes is None or minutes > settings.collector_max_gap_minutes:
        return "coleta atrasada" if minutes is not None else "nenhum ciclo registrado"
    return None


async def dispatch_collector(reason: str) -> bool:
    """Dispara o workflow de coleta pela API do GitHub com o token do próprio job."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return False
    url = f"https://api.github.com/repos/{repo}/actions/workflows/garimpo.yml/dispatches"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    import httpx
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, headers=headers, json={"ref": os.environ.get("GITHUB_REF_NAME") or "main"})
    if response.status_code >= 300:
        log.warning("Vigia não conseguiu disparar a coleta (HTTP %s)", response.status_code)
        return False
    log.info("Vigia disparou a coleta: %s", reason)
    return True


DAEMON_WORKFLOW = "robo.yml"


async def _github(method: str, path: str, **kwargs):
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return None
    import httpx
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=20) as client:
        return await client.request(method, f"https://api.github.com/repos/{repo}/{path}", headers=headers, **kwargs)


async def queued_runs() -> list[dict]:
    """Execuções do repositório esperando o runner (exceto a atual)."""
    try:
        response = await _github("GET", "actions/runs?status=queued&per_page=30")
        runs = response.json().get("workflow_runs", []) if response is not None and response.status_code < 300 else []
    except Exception:
        return []
    me = os.environ.get("GITHUB_RUN_ID", "")
    return [r for r in runs if str(r.get("id")) != me]


async def redispatch_daemon() -> bool:
    """Agenda a próxima execução do robô (se já não houver uma na fila)."""
    if any(str(r.get("path", "")).endswith(DAEMON_WORKFLOW) for r in await queued_runs()):
        return False
    response = await _github("POST", f"actions/workflows/{DAEMON_WORKFLOW}/dispatches",
                             json={"ref": os.environ.get("GITHUB_REF_NAME") or "main"})
    return response is not None and response.status_code < 300


async def _deliver_digest_now(totals: dict) -> None:
    """Se um horário de entrega venceu e ainda não foi entregue, entrega agora."""
    slot = digest_slot_due(datetime.now(timezone.utc))
    if slot is None or storage.get_state("last_digest").get("slot") == slot.isoformat():
        return
    metrics = {"alerts": 0}
    errors: list[str] = []
    await _deliver_digest(metrics, errors)
    totals["digests"] = totals.get("digests", 0) + (1 if metrics.get("digest") == "entregue" else 0)
    if errors:
        raise RuntimeError("; ".join(errors)[:200])


async def _daemon_cycle(reason: str) -> dict | None:
    log.info("Robô: iniciando coleta (%s)", reason)
    try:
        result = await asyncio.wait_for(run_cycle(), timeout=settings.cycle_timeout_seconds)
        log.info("Robô: coleta terminou como %s", result.get("outcome"))
        return result
    except Exception as exc:
        log.error("Robô: coleta falhou: %s", storage.safe_error(exc))
        return None


async def run_daemon(max_minutes: float) -> dict:
    """Robô sempre ligado: escuta os cliques (resposta em segundos) e roda coleta/entregas.

    Não depende do agendamento do GitHub. Ao chegar perto do limite de tempo do job, ou
    quando há outra execução esperando o runner (ex.: teste manual), termina a coleta em
    andamento, agenda a próxima execução de si mesmo e sai.
    """
    started = time.monotonic()
    deadline = started + max_minutes * 60
    totals = {"clicks": 0, "approved": 0, "discarded": 0, "sheet_ok": 0, "cycles": 0, "errors": 0}
    cycle: asyncio.Task | None = None
    last_queue_check = started
    last_retention = float("-inf")
    yielding = False
    while True:
        now = time.monotonic()
        if cycle is not None and cycle.done():
            totals["cycles"] += 1
            cycle = None
        if cycle is None and now - last_retention >= settings.retention_interval_minutes * 60:
            last_retention = now
            try:
                report = await retention_job()
                totals["retencao"] = report
                log.info("Robô: retenção 48 h: %s", json.dumps(report, ensure_ascii=False))
            except Exception as exc:
                totals["errors"] += 1
                log.warning("Robô: retenção adiada: %s", storage.safe_error(exc))
        if cycle is None:
            # Entrega das 08/12/18h na hora exata, com o que já foi classificado; não espera
            # um ciclo de coleta terminar. (Com ciclo rodando, o próprio ciclo entrega ao final.)
            try:
                await _deliver_digest_now(totals)
            except Exception as exc:
                totals["errors"] += 1
                log.warning("Robô: entrega adiada: %s", storage.safe_error(exc))
        if cycle is None and not yielding and now < deadline - settings.cycle_timeout_seconds:
            try:
                reason = collector_dispatch_reason()
            except Exception as exc:
                reason = None
                log.warning("Robô: vigia indisponível: %s", storage.safe_error(exc))
            if reason:
                cycle = asyncio.create_task(_daemon_cycle(reason))
        try:
            result = await process_buttons(poll_seconds=settings.daemon_poll_seconds)
            for key in ("clicks", "approved", "discarded", "sheet_ok"):
                totals[key] += result.get(key, 0)
        except Exception as exc:
            totals["errors"] += 1
            log.warning("Robô: leitura de cliques falhou: %s", storage.safe_error(exc))
            await asyncio.sleep(5)
        now = time.monotonic()
        if not yielding and now - last_queue_check >= settings.daemon_queue_check_seconds:
            last_queue_check = now
            others = [r for r in await queued_runs() if not str(r.get("path", "")).endswith(DAEMON_WORKFLOW)]
            if others:
                log.info("Robô: outra execução aguardando o runner; encerrando para dar passagem")
                yielding = True
        if (yielding or now >= deadline) and (cycle is None or cycle.done()):
            break
    if cycle is not None and cycle.done():
        totals["cycles"] += 1
    totals["redispatched"] = await redispatch_daemon()
    totals["minutes"] = round((time.monotonic() - started) / 60, 1)
    return totals


async def retention_job(vacuum_full: bool = False) -> dict:
    """Retenção de 48 h: marca no Telegram as mensagens que ficaram sem resposta e apaga do
    banco o que passou do prazo (aprovadas só depois de gravadas na planilha)."""
    hours = settings.retention_hours
    report: dict = {"mensagens_expiradas": 0}
    for opp in storage.expiring_messages(hours):
        try:
            await set_buttons(opp.get("telegram_chat_id"), opp.get("telegram_message_id"),
                              decided_keyboard("⌛ Expirou (48h)"))
            report["mensagens_expiradas"] += 1
        except TelegramError:
            pass
    report.update(storage.retention_cleanup(hours))
    storage.vacuum(full=vacuum_full)
    return report


def buttons_exit_code(result: dict) -> int:
    """Falha da planilha é tolerada (a aprovação fica na fila); vermelho só se persistir."""
    state = storage.get_state("sheet_failures")
    if result.get("sheet_failed"):
        streak = int(state.get("streak", 0)) + 1
        storage.set_state("sheet_failures", {"streak": streak, "error": result.get("sheet_error")})
        if streak >= settings.sheet_failure_streak_to_alert:
            log.error("Planilha falhou %s vezes seguidas: %s", streak, result.get("sheet_error"))
            return EXIT_DELIVERY_FAILED
    elif state:
        storage.set_state("sheet_failures", {})
    return EXIT_OK


def write_buttons_summary(result: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Botões Aprovar/Descartar", "", "| Métrica | Valor |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in result.items()]
    if result.get("sheet_not_configured"):
        lines += ["", "> Planilha ainda não configurada: as aprovações estão guardadas no banco e serão enviadas assim que SHEETS_WEBHOOK_URL e SHEETS_TOKEN existirem."]
    try:
        with open(path, "a", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
    except OSError:
        pass


def write_summary(result: dict | None, failure: str | None = None):
    """Resumo sanitizado no painel do GitHub Actions (somente métricas e erros sem segredos)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Garimpo ASPIN — resultado do ciclo", ""]
    if failure:
        lines += [f"**Resultado:** `failed`", "", f"**Causa:** {failure}"]
    elif result:
        outcome = result.get("outcome")
        lines += [f"**Resultado:** `{outcome}`", ""]
        if outcome == "success" and not result.get("opportunities"):
            lines += ["Busca concluída sem oportunidades novas acima do mínimo (isto é um resultado, não uma falha).", ""]
        rows = [("Janela desde", result.get("since")), ("Páginas DJEN", result.get("djen_pages")),
                ("Publicações DJEN", result.get("djen_documents")),
                ("Tribunais DataJud ok/total", f"{result.get('datajud_ok', 0)}/{result.get('tribunals', 0)}"),
                ("Sinais DataJud", result.get("datajud_documents")), ("Classificações IA ok/falhas", f"{result.get('ai_ok', 0)}/{result.get('ai_failed', 0)}"),
                ("Oportunidades novas", result.get("opportunities")), ("Alertas enviados", result.get("alerts")),
                ("Fila", json.dumps(result.get("queue", {}), ensure_ascii=False)),
                ("Duração (s)", json.dumps(result.get("duration_s", {}), ensure_ascii=False))]
        lines += ["| Métrica | Valor |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]
        if result.get("aviso_config"):
            lines += ["", f"> ⚠️ {result['aviso_config']}"]
        if result.get("errors"):
            lines += ["", "**Falhas registradas:**"] + [f"- {e}" for e in result["errors"][:20]]
    try:
        with open(path, "a", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description="Coletor ASPIN com fila persistente e diagnóstico explícito")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--check-db", action="store_true", help="testa a conexão PostgreSQL e mostra a causa sanitizada")
    parser.add_argument("--lookback-minutes", type=int)
    parser.add_argument("--simulate-failure", choices=SIMULATIONS, help="falha deliberada para validar o caminho de erro")
    parser.add_argument("--send-preview", action="store_true", help="envia ao canal uma prévia da entrega das 12h")
    parser.add_argument("--process-buttons", action="store_true", help="processa cliques Aprovar/Descartar e grava na planilha")
    parser.add_argument("--daemon", action="store_true", help="robô sempre ligado: cliques em tempo real + coleta/entregas")
    parser.add_argument("--maintenance", action="store_true", help="retenção de 48 h + compactação completa do banco")
    parser.add_argument("--max-minutes", type=float, default=340, help="duração máxima do robô antes de se reiniciar")
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.simulate_failure:
        settings.simulate_failure = args.simulate_failure
    try:
        if args.maintenance:
            require_configuration()
            storage.ensure_schema()
            before = storage.database_size_report()
            try:
                report = asyncio.run(retention_job(vacuum_full=True))
            except Exception as exc:
                # Mensagem do Postgres (sem DSN nem credenciais) ajuda a saber o que travou.
                detail = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
                log.error("Manutenção falhou: %s %s", type(exc).__name__, detail)
                raise
            after = storage.database_size_report()
            result = {"antes": before, "retencao": report, "depois": after}
            print(json.dumps(result, ensure_ascii=False, default=str))
            write_buttons_summary({"banco_antes_mb": before["total_mb"], "banco_depois_mb": after["total_mb"], **report})
            return EXIT_OK
        if args.daemon:
            require_configuration()
            storage.ensure_schema()
            totals = asyncio.run(run_daemon(args.max_minutes))
            print(json.dumps(totals, ensure_ascii=False))
            write_buttons_summary(totals)
            return EXIT_OK
        if args.process_buttons:
            require_configuration()
            storage.ensure_schema()
            result = asyncio.run(process_buttons())
            try:
                reason = collector_dispatch_reason()
                if reason:
                    result["collector_dispatched"] = asyncio.run(dispatch_collector(reason))
                    result["collector_dispatch_reason"] = reason
            except Exception as exc:
                log.warning("Vigia indisponível: %s", storage.safe_error(exc))
            print(json.dumps(result, ensure_ascii=False))
            write_buttons_summary(result)
            return buttons_exit_code(result)
        if args.send_preview:
            require_configuration()
            storage.ensure_schema()
            return asyncio.run(send_preview())
        if args.check_db:
            if not settings.database_url.strip():
                raise CycleFailure("Configuração incompleta: DATABASE_URL")
            print(storage.check_connection())
            return EXIT_OK
        require_configuration()
        if args.check_config:
            print("Configuração obrigatória presente. Conexões ainda precisam de validação online.")
            return EXIT_OK
        if args.once:
            result = asyncio.run(asyncio.wait_for(run_cycle(args.lookback_minutes), timeout=settings.cycle_timeout_seconds))
            print(json.dumps(result, ensure_ascii=False, default=str))
            write_summary(result)
            code = OUTCOME_EXIT.get(result["outcome"], EXIT_UNEXPECTED)
            if code != EXIT_OK:
                log.error("Ciclo terminou como %s (exit %s): %s", result["outcome"], code, "; ".join(result["errors"][:5]))
            return code
        asyncio.run(run_forever())
        return EXIT_OK
    except CycleFailure as exc:
        log.error("%s", exc)
        write_summary(None, str(exc))
        return EXIT_CONFIG
    except (TimeoutError, asyncio.TimeoutError):
        message = f"Tempo do ciclo esgotado ({settings.cycle_timeout_seconds}s); o ciclo foi registrado como failed e a fila segue para o próximo"
        log.error("%s", message)
        write_summary(None, message)
        return EXIT_UNEXPECTED
    except storage.DatabaseUnavailable as exc:
        log.error("Banco inacessível. %s", exc)
        write_summary(None, f"Banco inacessível. {exc}")
        return EXIT_DATABASE
    except Exception as exc:
        log.error("Execução falhou: %s", storage.safe_error(exc))
        write_summary(None, storage.safe_error(exc))
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
