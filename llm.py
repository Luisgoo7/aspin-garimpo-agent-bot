import asyncio
import json
import logging
from typing import Any
import httpx
from .config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Você classifica evidências processuais públicas para a ASPIN Consultoria Empresarial.
Nunca invente dados. Se um campo não estiver presente, use null. Diferencie ofício requisitório,
RPV e precatório. Responda somente JSON válido no formato:
{"is_opportunity": boolean, "requisition_type": "oficio_requisitorio|rpv|precatorio|outro|null",
"amount": number|null, "beneficiary": string|null, "debtor_entity": string|null,
"lawyer": string|null, "issued_at": string|null, "deadline_at": string|null,
"potential_score": number, "reason": string, "evidence_quote": string}
O campo reason deve estar em português do Brasil, ser objetivo e ter no máximo duas frases.
"""


class LLMClassifier:
    """Tenta Groq e depois NVIDIA. Falhas sao registradas no log (antes eram silenciosas).
    Se todos falharem, devolve {"error": True} para o worker contar como erro, nao como 'sem oportunidade'."""

    async def classify(self, evidence: str) -> dict[str, Any]:
        errors = []
        if settings.groq_api_key:
            try:
                return await self._groq(evidence)
            except Exception as exc:
                log.error("Groq falhou: %r", exc)
                errors.append(f"groq: {exc!r}")
        if settings.nvidia_api_key:
            try:
                return await self._nvidia(evidence)
            except Exception as exc:
                log.error("NVIDIA falhou: %r", exc)
                errors.append(f"nvidia: {exc!r}")
        if not errors:
            errors.append("nenhum provedor de IA configurado")
        return {"error": True, "is_opportunity": False, "potential_score": 0.0, "reason": "; ".join(errors), "evidence_quote": ""}

    async def _post(self, url: str, payload: dict, headers: dict) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=45) as client:
            for attempt in range(2):
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code == 429:
                    raise RuntimeError("limite de uso do provedor (HTTP 429)")
                r.raise_for_status()
                return json.loads(r.json()["choices"][0]["message"]["content"])
        raise RuntimeError("falha apos retentativa")

    async def _groq(self, evidence: str) -> dict[str, Any]:
        payload = {"model": "openai/gpt-oss-20b", "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": evidence}]}
        return await self._post("https://api.groq.com/openai/v1/chat/completions", payload, {"Authorization": f"Bearer {settings.groq_api_key}"})

    async def _nvidia(self, evidence: str) -> dict[str, Any]:
        payload = {"model": "meta/llama-3.1-8b-instruct", "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": evidence}]}
        return await self._post("https://integrate.api.nvidia.com/v1/chat/completions", payload, {"Authorization": f"Bearer {settings.nvidia_api_key}", "Accept": "application/json"})
