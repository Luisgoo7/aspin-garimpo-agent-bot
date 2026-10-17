import argparse
import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

from .config import settings
from .datajud import DataJudClient
from .llm import LLMClassifier
from .opportunity import KEYWORDS, canonical_alias, canonical_cnj, clean_text, parse_amount
from .sources import search_djen, probe_tjsp_pages
from .telegram_bot import send_alert
from . import storage

log = logging.getLogger(__name__)


class CycleFailure(RuntimeError):
    pass


def require_configuration():
    missing = settings.configuration_errors()
    if missing:
        raise CycleFailure("Configuração incompleta: " + ", ".join(missing))


def build_query(since: datetime) -> dict:
    return {
        "size": settings.datajud_page_size,
        "track_total_hits": True,
        "_source": ["numeroProcesso", "movimentos", "classe", "assuntos", "dataHoraUltimaAtualizacao", "url"],
        "sort": [{"dataHoraUltimaAtualizacao": {"order": "desc", "unmapped_type": "date"}}],
        "query": {"bool": {
            "filter": [{"range": {"movimentos.dataHora": {"gte": since.isoformat()}}}],
            "should": [{"match_phrase": {"movimentos.nome": term}} for term in KEYWORDS],
            "minimum_should_match": 1,
        }},
    }


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
        f"Confiança da classificação: {float(opp['potential_score']):.0%}\n\n"
        f"Motivo: {str(result.get('reason') or '')[:400]}\n\n"
        f"Evidência: {str(opp.get('text_excerpt') or '')[:700]}\n\n"
        f"Fonte: {str(url)[:600]}\n\n"
        "Revisão comercial e jurídica necessária: confirmar situação atual, titularidade e disponibilidade. Comissão é estimativa, não retorno garantido."
    )[:4000]


def status_message(metrics: dict, errors: list[str]) -> str:
    queue = metrics.get("queue", {})
    pending = sum(queue.get(k, 0) for k in ("pending", "retry", "processing"))
    if errors:
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
        f"Classificações neste ciclo: {metrics.get('classified', 0)}\n"
        f"Candidatos novos com valor mínimo: {metrics.get('opportunities', 0)}\n"
        f"Alertas enviados: {metrics.get('alerts', 0)}\n"
        f"Fila pendente: {pending}\n"
        f"Aguardando enriquecimento: {queue.get('needs_enrichment', 0)}\n"
        f"Falhas aguardando revisão: {queue.get('failed', 0)}"
    )
    if errors:
        text += "\n\nFalhas: " + "; ".join(errors[:4])[:800]
    elif not metrics.get("opportunities"):
        text += "\n\nNenhum candidato novo aprovado nesta execução; isso não significa ausência de créditos no mercado."
    return text


async def maybe_send_status(metrics: dict, errors: list[str]):
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


async def _collect_datajud(since: datetime, metrics: dict, errors: list[str]):
    if not settings.enable_datajud:
        metrics["datajud_disabled"] = True
        return
    client = DataJudClient()
    aliases = [canonical_alias(x) for x in settings.datajud_tribunals.split(",") if x.strip()]
    for alias in aliases:
        metrics["tribunals"] += 1
        try:
            data = await client.search(alias, build_query(since))
            hits_data = data.get("hits", {})
            hits = hits_data.get("hits", [])
            total = hits_data.get("total", {})
            total = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
            if total > len(hits):
                metrics["limited"] = True
            for hit in hits:
                source = hit.get("_source") or {}
                cnj = canonical_cnj(source.get("numeroProcesso") or "")
                if not cnj:
                    continue
                storage.upsert_document({
                    "source": "datajud", "external_id": alias + ":" + cnj,
                    "siglaTribunal": alias, "numeroProcesso": cnj, "link": source.get("url"),
                    "texto": json.dumps(source, ensure_ascii=False), "metadata": source,
                })
                metrics["datajud_documents"] += 1
        except Exception as exc:
            errors.append(f"datajud {alias}: {storage.safe_error(exc)}")
            log.error("DataJud %s indisponível: %s", alias, storage.safe_error(exc))


async def _process_queue(metrics: dict, errors: list[str]):
    classifier = LLMClassifier()
    for _ in range(settings.max_classify_per_cycle):
        doc = storage.claim_document()
        if not doc:
            break
        try:
            raw = doc.get("raw_data") or {}
            if raw.get("motivo_cancelamento") or raw.get("motivoCancelamento"):
                storage.finish_document(doc, "cancelled")
                continue
            text = clean_text(doc.get("text_excerpt") or "")
            # Nunca marca como concluído antes da resposta da IA e persistência.
            result = await classifier.classify(text[:18000])
            metrics["classified"] += 1
            if result.get("error"):
                reason = str(result.get("reason") or "provedores indisponíveis")[:240]
                storage.finish_document(doc, "retry", error=reason)
                errors.append("ia: " + reason)
                # Circuit breaker: não esgota todos os documentos se provedores estão indisponíveis.
                break
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


async def _send_pending_alerts(metrics: dict, errors: list[str]):
    for opp in storage.pending_alerts(settings.max_classify_per_cycle):
        try:
            await send_alert(format_alert(opp))
            storage.mark_alerted(opp["id"])
            metrics["alerts"] += 1
        except Exception as exc:
            errors.append("telegram: " + storage.safe_error(exc))
            log.error("Alerta continua pendente: %s", storage.safe_error(exc))
            break


async def run_cycle(lookback_minutes: int | None = None) -> dict:
    require_configuration()
    with storage.collector_lock() as locked:
        if not locked:
            log.warning("Outro coletor já está executando; ciclo não duplicado")
            return {"outcome": "skipped", "opportunities": 0, "errors": []}
        storage.ensure_schema()
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
                   "datajud_documents": 0, "tribunals": 0, "classified": 0,
                   "opportunities": 0, "alerts": 0, "below_minimum": 0, "limited": False}
        errors = []
        run_id = storage.start_run()
        try:
            cursor = storage.get_state("djen_cursor") or None
            query_since = date.fromisoformat(cursor["since"]) if cursor else since.date()
            query_until = date.fromisoformat(cursor["until"]) if cursor else now.date()
            batch = await search_djen(query_since, query_until, cursor=cursor)
            metrics["djen_documents"] = len(batch.rows)
            metrics["djen_pages"] = batch.pages
            metrics["djen_totals"] = batch.totals
            metrics["limited"] = batch.limited
            errors.extend(batch.errors)
            for row in batch.rows:
                storage.upsert_document(row)
            storage.set_state("djen_cursor", batch.cursor or {})
            await _collect_datajud(since, metrics, errors)
            if settings.enable_tjsp_probe:
                # Monitoramento explicitamente separado da descoberta.
                probes = await probe_tjsp_pages()
                metrics["tjsp_probe_available"] = len(probes)
            await _process_queue(metrics, errors)
            await _send_pending_alerts(metrics, errors)
            metrics["queue"] = storage.queue_counts()
            try:
                await maybe_send_status(metrics, errors)
            except Exception as exc:
                errors.append("status_telegram: " + storage.safe_error(exc))
            outcome = "degraded" if errors else "partial" if metrics["limited"] else "success"
            if not errors and not metrics["limited"]:
                covered = min(now, datetime.combine(query_until + timedelta(days=1), datetime.min.time(), timezone.utc))
                storage.set_state("last_collection", {"at": covered.isoformat()})
            storage.set_state("last_health", {"at": datetime.now(timezone.utc).isoformat(), "outcome": outcome})
            storage.finish_run(run_id, outcome, metrics, errors)
            log.info("Ciclo concluído: %s", json.dumps({"outcome": outcome, **metrics, "errors": errors}, ensure_ascii=False))
            return {"outcome": outcome, **metrics, "errors": errors}
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                error = "timeout_ou_interrupcao"
            else:
                error = storage.safe_error(exc)
            errors.append("ciclo: " + error)
            storage.finish_run(run_id, "failed", metrics, errors)
            raise


async def poll_once(lookback_minutes: int | None = None) -> int:
    result = await run_cycle(lookback_minutes)
    if result["errors"]:
        raise CycleFailure("Ciclo com falhas: " + "; ".join(result["errors"][:5]))
    return result["opportunities"]


async def run_forever():
    require_configuration()
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(run_cycle(), timeout=settings.cycle_timeout_seconds)
        except Exception as exc:
            log.error("Ciclo não concluído: %s", storage.safe_error(exc))
        delay = settings.poll_interval_minutes * 60 - (asyncio.get_running_loop().time() - started)
        await asyncio.sleep(max(0, delay))


def main():
    parser = argparse.ArgumentParser(description="Coletor ASPIN com fila persistente e diagnóstico explícito")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--lookback-minutes", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        require_configuration()
        if args.check_config:
            print("Configuração obrigatória presente. Conexões ainda precisam de validação online.")
            return 0
        if args.once:
            result = asyncio.run(asyncio.wait_for(run_cycle(args.lookback_minutes), timeout=settings.cycle_timeout_seconds))
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result["errors"] else 0
        asyncio.run(run_forever())
        return 0
    except CycleFailure as exc:
        log.error("%s", exc)
        return 2
    except Exception as exc:
        log.error("Execução falhou: %s", storage.safe_error(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
