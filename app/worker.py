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
from .opportunity import KEYWORDS, max_amount_in_text, canonical_alias, canonical_cnj, clean_text, parse_amount
from .sources import search_djen, probe_tjsp_pages
from .telegram_bot import send_alert
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


def format_alert(opp: dict) -> str:
    result = opp.get("classification") or {}
    basis = {"principal": "Principal", "bruto": "Bruto", "liquido": "Líquido", "nao_informado": "Base não informada"}.get(result.get("amount_basis"), "Base não informada")
    tribunal = str(opp.get("alias") or "não informado").removeprefix("api_publica_").upper()
    url = opp.get("document_url") or opp.get("source_url") or "Link não fornecido pela fonte"
    return (
        "ASPIN | Candidato judicial identificado\n\n"
        f"Tribunal: {tribunal}\n"
        f"Tipo: {opp.get('requisition_type') or 'não informado'}\n"
        f"Valor citado ({basis}): {brl(opp['amount'])}\n"
        f"Comissão estimada ASPIN ({settings.aspin_commission_rate:.0%}): {brl(float(opp['amount']) * settings.aspin_commission_rate)}\n"
        f"Beneficiário: {opp.get('beneficiary') or 'não informado'}\n"
        f"Devedor: {opp.get('debtor_entity') or 'não informado'}\n"
        f"Processo: {opp.get('cnj_number') or 'não informado'}\n"
        f"Confiança da classificação: {float(opp['potential_score']):.0%}\n"
        f"{_datajud_line(opp)}\n\n"
        f"Motivo: {str(result.get('reason') or '')[:400]}\n\n"
        f"Evidência: {str(opp.get('text_excerpt') or '')[:700]}\n\n"
        f"Fonte: {str(url)[:600]}\n\n"
        "Revisão comercial e jurídica necessária: confirmar situação atual, titularidade e disponibilidade. Comissão é estimativa, não retorno garantido."
    )[:4000]


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
        metrics["tribunals"] += 1
        key = "datajud_cursor:" + alias
        try:
            if simulated("datajud"):
                raise DataJudError("falha simulada")
            rows, cursor, info = await collect_expeditions(alias, storage.get_state(key))
            for row in rows:
                storage.upsert_document(row)
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
    deadline = time.monotonic() + settings.ai_time_budget_seconds
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
                result = await classifier.classify(text[:18000])
            ai_calls += 1
            metrics["classified"] += 1
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
    best = storage.pending_alerts(max(0, settings.digest_top_n - len(sent_ids))) if len(sent_ids) < settings.digest_top_n else []
    if not best and not sent_ids:
        if simulated("telegram"):
            raise RuntimeError("Telegram: falha simulada")
        await send_alert(digest_message_empty(slot, metrics))
    elif best:
        before = metrics.get("alerts", 0)
        await _send_pending_alerts(metrics, errors, best)
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


async def _send_pending_alerts(metrics: dict, errors: list[str], opportunities: list[dict] | None = None):
    if opportunities is None:
        opportunities = storage.pending_alerts(settings.max_classify_per_cycle)
    for opp in opportunities:
        try:
            if simulated("telegram"):
                raise RuntimeError("Telegram: falha simulada")
            try:
                opp["datajud_signal"] = storage.datajud_signal(opp.get("cnj_number"))
            except Exception:
                opp["datajud_signal"] = None
            await send_alert(format_alert(opp))
            storage.mark_alerted(opp["id"])
            metrics["alerts"] += 1
        except Exception as exc:
            reason = str(exc) if str(exc).startswith("Telegram") else storage.safe_error(exc)
            errors.append("telegram: " + reason)
            metrics["alerts_failed"] = metrics.get("alerts_failed", 0) + 1
            log.error("Alerta continua pendente: %s", reason)
            break


async def run_cycle(lookback_minutes: int | None = None) -> dict:
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
                for row in batch.rows:
                    storage.upsert_document(row)
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
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.simulate_failure:
        settings.simulate_failure = args.simulate_failure
    try:
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
