import asyncio
import json
import logging
from typing import Any
import httpx
from .config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Você é um analista sênior de oportunidades processuais para a ASPIN Consultoria Empresarial.
Analise somente o texto fornecido e nunca invente dados. Uma oportunidade válida precisa indicar,
com evidência textual, a existência de crédito ou pagamento judicial potencialmente negociável,
como ofício requisitório, RPV ou precatório. Não considere oportunidade: simples citação de lei,
modelo de petição, intimação sem crédito, consulta processual, cálculo sem ordem de pagamento,
notícia genérica, parte que apenas menciona precatório de terceiro ou texto sem valor identificável.
O campo amount deve ser o valor monetário explicitamente associado ao crédito; se houver vários
valores, escolha o valor líquido do crédito quando o texto permitir e explique a escolha. Se não
houver valor confiável, use null e is_opportunity=false. Diferencie ofício requisitório, RPV e
precatório. Responda somente JSON válido no formato:
{"is_opportunity": boolean, "requisition_type": "oficio_requisitorio|rpv|precatorio|outro|null",
"amount": number|null, "beneficiary": string|null, "debtor_entity": string|null,
"lawyer": string|null, "issued_at": string|null, "deadline_at": string|null,
"potential_score": number, "reason": string, "evidence_quote": string}
potential_score deve ser um número entre 0 e 1. Só use is_opportunity=true quando houver
evidência direta e valor identificável. O evidence_quote deve ser uma cópia curta e literal do
trecho decisivo. O campo reason deve estar em português do Brasil, ser objetivo e ter no máximo
duas frases. Não inclua markdown nem texto fora do JSON.
"""


class LLMClassifier:
    """Tenta Groq e depois NVIDIA. Falhas sao registradas no log (antes eram silenciosas).
    Se todos falharem, devolve {"error": True} para o worker contar como erro, nao como 'sem oportunidade'."""

    async def classify(self, evidence: str) -> dict[str, Any]:
        errors = []
        if settings.groq_api_key:
            try:
                return self._normalize(await self._groq(evidence))
            except Exception as exc:
                log.error("Groq falhou: %r", exc)
                errors.append(f"groq: {exc!r}")
        if settings.nvidia_api_key:
            try:
                return self._normalize(await self._nvidia(evidence))
            except Exception as exc:
                log.error("NVIDIA falhou: %r", exc)
                errors.append(f"nvidia: {exc!r}")
        if not errors:
            errors.append("nenhum provedor de IA configurado")
        return {"error": True, "is_opportunity": False, "potential_score": 0.0, "reason": "; ".join(errors), "evidence_quote": ""}

    def _normalize(self, result: dict[str, Any]) -> dict[str, Any]:
        required = ("is_opportunity", "potential_score", "reason", "evidence_quote")
        if not isinstance(result, dict) or any(key not in result for key in required):
            raise ValueError("resposta da IA sem campos obrigatorios")
        try:
            score = float(result.get("potential_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score > 1:
            score /= 100
        result["potential_score"] = max(0.0, min(1.0, score))
        result["is_opportunity"] = bool(result.get("is_opportunity"))
        return result

    async def _post(self, url: str, payload: dict, headers: dict) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=45) as client:
            for attempt in range(3):
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code == 429:
                    retry_after = r.headers.get("retry-after", "2")
                    try:
                        delay = min(30, int(retry_after))
                    except ValueError:
                        delay = 2 ** attempt
                    await asyncio.sleep(delay)
                    continue
                if r.status_code >= 500 and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                if content.startswith("```"):
                    content = content.strip().removeprefix("```json").removesuffix("```").strip()
                return json.loads(content)
        raise RuntimeError("falha apos tres tentativas")

    async def _groq(self, evidence: str) -> dict[str, Any]:
        payload = {"model": "openai/gpt-oss-20b", "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": evidence}]}
        return await self._post("https://api.groq.com/openai/v1/chat/completions", payload, {"Authorization": f"Bearer {settings.groq_api_key}"})

    async def _nvidia(self, evidence: str) -> dict[str, Any]:
        payload = {"model": "meta/llama-3.1-8b-instruct", "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": evidence}]}
        return await self._post("https://integrate.api.nvidia.com/v1/chat/completions", payload, {"Authorization": f"Bearer {settings.nvidia_api_key}", "Accept": "application/json"})
