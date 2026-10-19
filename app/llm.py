import asyncio
import html
import json
import logging
import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .config import settings

log = logging.getLogger(__name__)

DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_NVIDIA_MODEL = "openai/gpt-oss-20b"
MAX_RETRIES = 3
MAX_RETRY_DELAY = 15.0
# Provedor que esgota as tentativas com 429 fica em pausa, em vez de travar o ciclo.
RATE_LIMIT_COOLDOWN_SECONDS = 600
_COOLDOWN_UNTIL: dict[str, float] = {}

SYSTEM_PROMPT = """Você é um analista sênior de oportunidades processuais para a ASPIN Consultoria Empresarial.
Analise somente o texto fornecido e nunca invente dados. Uma publicação é apenas um candidato para
análise, não uma oportunidade comercial garantida.

Só marque is_opportunity=true quando o próprio texto trouxer evidência literal de crédito
explicitamente ordenado, requisitado ou expedido (por exemplo, ofício requisitório, requisição de
pagamento, RPV expedida ou precatório expedido) E trouxer um valor monetário associado a esse
crédito. Uma simples menção a precatório, RPV, lei, petição, intimação, consulta processual,
cálculo, notícia, parte ou crédito de terceiro não basta. Uma publicação sem valor deve ser
rejeitada como oportunidade, mas marque need_enrichment=true se houver sinal plausível de crédito
requisitado/expedido que possa ser enriquecido depois.

O campo amount deve ser exclusivamente um valor monetário que apareça no texto. Nunca calcule,
invente ou deduza valor. Normalize valores brasileiros (R$ 1.234,56) para número finito em reais.
Se houver principal e honorários separados, selecione somente o valor PRINCIPAL do beneficiário.
NUNCA some principal e honorários, nem some valores de diferentes beneficiários. O número final
precisa aparecer literalmente no texto. Datas de movimentação não são datas de expedição:
issued_at e deadline_at devem ser null quando sua associação não estiver expressa.
Se não houver valor explícito, use null. amount_basis distingue o que o texto realmente diz:
principal, bruto, liquido ou nao_informado. Use liquido somente se o texto disser que o valor é
líquido ou equivalente após descontos; não rotule todos os valores como líquidos. Use bruto ou
principal somente quando houver essa indicação explícita. Caso contrário, use nao_informado.

Responda somente JSON válido, sem markdown, no formato:
{
  "is_opportunity": boolean,
  "need_enrichment": boolean,
  "requisition_type": "oficio_requisitorio|rpv|precatorio|outro|null",
  "amount": number|null,
  "amount_basis": "principal|bruto|liquido|nao_informado",
  "beneficiary": string|null,
  "debtor_entity": string|null,
  "lawyer": string|null,
  "issued_at": string|null,
  "deadline_at": string|null,
  "potential_score": number,
  "reason": string,
  "evidence_quote": string
}
potential_score deve ser um número finito entre 0 e 1; se usar percentual, informe 0 a 100.
O evidence_quote deve ser uma cópia curta, literal, CONTÍNUA e decisiva do texto, sem reticências,
sem juntar frases distantes e sem paráfrase. O reason deve estar em
português do Brasil, ser objetivo e ter no máximo duas frases. Não inclua texto fora do JSON.
"""


class _ProviderError(RuntimeError):
    """Erro interno sem URL, cabeçalho ou corpo potencialmente sensível."""

    def __init__(self, categoria: str, status: int | None = None):
        self.categoria = categoria
        self.status = status
        mensagem = categoria if status is None else f"{categoria} HTTP {status}"
        super().__init__(mensagem)


def _setting(name: str, default: str) -> str:
    valor = getattr(settings, name, None)
    if valor is None:
        return default
    valor = str(valor).strip()
    return valor or default


def _limpar_texto(valor: Any) -> str:
    if valor is None:
        return ""
    texto = html.unescape(str(valor)).replace("\u00a0", " ")
    texto = re.sub(r"(?is)<br\s*/?>", " ", texto)
    texto = re.sub(r"(?is)<[^>]*>", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _sem_acentos(valor: Any) -> str:
    texto = _limpar_texto(valor).casefold()
    return "".join(
        caractere
        for caractere in unicodedata.normalize("NFKD", texto)
        if not unicodedata.combining(caractere)
    )


def _normalize_evidence_text(valor: Any) -> str:
    """Normaliza somente HTML e espaços para comparação literal de evidência."""

    return _limpar_texto(valor)


def _evidence_is_literal(evidence_quote: Any, source_text: Any) -> bool:
    """Retorna se a citação é um trecho literal após normalizar HTML e espaços."""

    citacao = _normalize_evidence_text(evidence_quote)
    fonte = _normalize_evidence_text(source_text)
    return bool(citacao) and citacao.casefold() in fonte.casefold()


def _repair_evidence_quote(value: Any, source_text: str) -> str:
    quote_text = _normalize_evidence_text(value)
    source = _normalize_evidence_text(source_text)
    if _evidence_is_literal(quote_text, source):
        return quote_text
    parts = [p.strip() for p in re.split(r"\.{3}|…", quote_text) if p.strip()]
    if not 2 <= len(parts) <= 3 or any(len(p) < 12 for p in parts):
        return quote_text
    position = 0
    first = None
    for part in parts:
        index = source.casefold().find(part.casefold(), position)
        if index < 0:
            return quote_text
        if first is None:
            first = index
        position = index + len(part)
    if position - first > 1200:
        return quote_text
    # Retorna exclusivamente um trecho do documento original, sem gerar palavras.
    return source[first:position]


def _strict_bool(valor: Any) -> bool:
    """Converte apenas valores booleanos ou literais conhecidos, nunca por truthiness."""

    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        token = _sem_acentos(valor)
        if token in {"true", "verdadeiro", "sim", "yes", "1"}:
            return True
        if token in {"false", "falso", "nao", "não", "no", "0", ""}:
            return False
    return False


def _normalize_score(valor: Any) -> float:
    try:
        if isinstance(valor, str):
            texto = valor.strip().replace("%", "").replace(",", ".")
        else:
            texto = valor
        score = float(texto)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    if score > 1.0:
        score /= 100.0
    if not math.isfinite(score):
        return 0.0
    return min(1.0, max(0.0, score))


def _normalize_amount(valor: Any) -> float | None:
    """Converte valores em formato brasileiro sem aceitar float infinito ou texto inventado."""

    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        numero = float(valor)
        return numero if math.isfinite(numero) else None

    texto = html.unescape(str(valor)).strip()
    texto = re.sub(r"(?i)(?:r\$|brl|reais?)", "", texto)
    texto = texto.replace("\u00a0", " ").replace(" ", "")
    if not texto or not re.fullmatch(r"[+-]?\d[\d.,]*", texto):
        return None

    negativo = texto.startswith("-")
    texto = texto.lstrip("+-")
    if "," in texto:
        inteiro, fracao = texto.rsplit(",", 1)
        if not fracao.isdigit() or len(fracao) > 2:
            return None
        inteiro = inteiro.replace(".", "") or "0"
        canonico = f"{inteiro}.{fracao}"
    elif texto.count(".") > 1:
        canonico = texto.replace(".", "")
    elif "." in texto:
        inteiro, fracao = texto.split(".", 1)
        # Em pt-BR um grupo de três dígitos depois do ponto é milhar.
        if len(fracao) == 3 and inteiro:
            canonico = inteiro + fracao
        else:
            canonico = texto
    else:
        canonico = texto

    try:
        numero = float(canonico)
    except (TypeError, ValueError, OverflowError):
        return None
    if negativo:
        numero = -numero
    return numero if math.isfinite(numero) else None


def _normalize_requisition_type(valor: Any) -> str | None:
    if valor is None:
        return None
    token = _sem_acentos(valor).replace("-", " ").replace("_", " ")
    if not token or token in {"null", "none", "nao informado", "desconhecido"}:
        return None
    if "rpv" in token or "requisicao de pequeno valor" in token:
        return "rpv"
    if "precatorio" in token:
        return "precatorio"
    if "oficio" in token and "requisitorio" in token:
        return "oficio_requisitorio"
    if "outro" in token:
        return "outro"
    return "outro"


def _normalize_amount_basis(valor: Any) -> str:
    token = _sem_acentos(valor).replace("-", " ").replace("_", " ")
    if re.search(r"\bliquid(?:o|a|os|as)\b", token) or "apos desconto" in token or "apos deduc" in token:
        return "liquido"
    if "brut" in token:
        return "bruto"
    if "principal" in token:
        return "principal"
    return "nao_informado"


def _has_credit_signal(texto: Any) -> bool:
    """Detecta ação de pagamento/requisição, não a mera menção a um instituto."""

    texto_normalizado = _sem_acentos(texto)
    padroes = (
        r"\boficio\s+requisitorio\b",
        r"\brequisicao\s+de\s+pagamento\b",
        r"\bordem\s+de\s+pagamento\b",
        r"\b(?:rpv|precatorio)\s+(?:foi\s+)?expedid\w*\b",
        r"\bexpedid\w*\s+(?:a\s+|o\s+|de\s+)?(?:rpv|precatorio)\b",
        r"\bexpedicao\s+(?:de\s+)?(?:rpv|precatorio)\b",
        r"\b(?:rpv|precatorio)\s+requisitad\w*\b",
        r"\bvalor\s+requisitad\w*\b",
        r"\brequisicao\s+expedid\w*\b",
    )
    return any(re.search(padrao, texto_normalizado) for padrao in padroes)


_AMOUNT_CONTEXT_RE = re.compile(
    r"(?ix)(?:r\$|brl|\b(?:valor|montante)\b[^\d]{0,24})\s*"
    r"([+-]?\d[\d.\s]*(?:,\d{1,2})?)"
)


def _amount_appears_in_text(amount: float | None, texto: Any) -> bool:
    if amount is None or not math.isfinite(amount):
        return False
    fonte = _normalize_evidence_text(texto)
    for match in _AMOUNT_CONTEXT_RE.finditer(fonte):
        candidato = _normalize_amount(match.group(1))
        if candidato is not None and math.isclose(candidato, amount, rel_tol=0.0, abs_tol=0.01):
            return True
    return False


def _basis_is_supported(basis: str, texto: Any) -> bool:
    if basis == "nao_informado":
        return True
    normalizado = _sem_acentos(texto)
    if basis == "liquido":
        return bool(
            re.search(
                r"\bliquid(?:o|a|os|as)\b|apos\s+(?:os\s+)?descont\w*|apos\s+(?:as\s+)?deduc\w*",
                normalizado,
            )
        )
    if basis == "bruto":
        return bool(re.search(r"\bbrut\w*\b", normalizado))
    if basis == "principal":
        return bool(re.search(r"\bprincipal\b", normalizado))
    return False


def _decode_json_content(conteudo: Any) -> dict[str, Any]:
    """Aceita JSON puro, fences com linguagem variável e texto incidental ao redor."""

    if isinstance(conteudo, dict):
        return conteudo
    if isinstance(conteudo, list):
        partes = []
        for parte in conteudo:
            if isinstance(parte, dict) and isinstance(parte.get("text"), str):
                partes.append(parte["text"])
            elif isinstance(parte, str):
                partes.append(parte)
        conteudo = "".join(partes)
    if not isinstance(conteudo, str):
        raise _ProviderError("conteúdo JSON inválido")

    texto = conteudo.lstrip("\ufeff").strip()
    candidatos = [texto]
    for match in re.finditer(r"```(?:\s*json)?\s*(.*?)\s*```", texto, flags=re.IGNORECASE | re.DOTALL):
        candidatos.insert(0, match.group(1).strip())

    decodificador = json.JSONDecoder()
    for candidato in candidatos:
        try:
            resultado = json.loads(candidato)
            if isinstance(resultado, dict):
                return resultado
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        for indice, caractere in enumerate(candidato):
            if caractere not in "{[":
                continue
            try:
                resultado, _ = decodificador.raw_decode(candidato[indice:])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(resultado, dict):
                return resultado
    raise _ProviderError("conteúdo JSON inválido")


def _openai_content(corpo: Any) -> Any:
    try:
        content = corpo["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _ProviderError("resposta OpenAI incompatível") from exc
    return _decode_json_content(content)


def _gemini_content(corpo: Any) -> dict[str, Any]:
    try:
        partes = corpo["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _ProviderError("resposta Gemini incompatível") from exc
    textos = [parte.get("text", "") for parte in partes if isinstance(parte, dict)]
    return _decode_json_content("".join(textos))


def _safe_error(exc: BaseException) -> str:
    """Representa falhas sem repr de exceções HTTP, URL, query string ou cabeçalhos."""

    if isinstance(exc, _ProviderError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        resposta = exc.response
        status = getattr(resposta, "status_code", None)
        return f"HTTP {status}" if status is not None else "HTTP sem status"
    if isinstance(exc, httpx.RequestError):
        return "erro de rede"
    return type(exc).__name__


def _requested_delay(response: httpx.Response | Any) -> float | None:
    valor = getattr(response, "headers", {}).get("retry-after")
    try:
        return float(str(valor).strip()) if valor else None
    except (TypeError, ValueError):
        return None


def _retry_delay(response: httpx.Response | Any, attempt: int) -> float:
    valor = getattr(response, "headers", {}).get("retry-after")
    if valor:
        try:
            atraso = float(str(valor).strip())
        except (TypeError, ValueError):
            try:
                data = parsedate_to_datetime(str(valor))
                if data.tzinfo is None:
                    data = data.replace(tzinfo=timezone.utc)
                atraso = (data - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                atraso = 2**attempt
    else:
        atraso = 2**attempt
    if not math.isfinite(atraso):
        atraso = 2**attempt
    return min(MAX_RETRY_DELAY, max(0.0, atraso))


class LLMClassifier:
    """Classifica com fallback Groq, Gemini nativo e NVIDIA, nessa ordem."""

    async def classify(self, evidence: str) -> dict[str, Any]:
        erros: list[str] = []
        provedores: list[tuple[str, str, Callable[[str], Any]]] = [
            ("Groq", getattr(settings, "groq_api_key", ""), self._groq),
            ("Gemini", getattr(settings, "gemini_api_key", ""), self._gemini),
            ("NVIDIA", getattr(settings, "nvidia_api_key", ""), self._nvidia),
        ]
        for nome, chave, chamada in provedores:
            if not str(chave or "").strip():
                continue
            if _COOLDOWN_UNTIL.get(nome, 0) > time.monotonic():
                erros.append(f"{nome.lower()}: em pausa após limite 429")
                continue
            try:
                bruto = await chamada(evidence)
                normalized = self._normalize(bruto, evidence)
                if normalized.get("validation_error"):
                    raise _ProviderError("resposta sem evidência ou valor literal")
                return normalized
            except Exception as exc:
                mensagem = _safe_error(exc)
                if getattr(exc, "status", None) == 429:
                    _COOLDOWN_UNTIL[nome] = time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
                log.error("%s falhou: %s", nome, mensagem)
                erros.append(f"{nome.lower()}: {mensagem}")

        if not erros:
            erros.append("nenhum provedor de IA configurado")
        return {
            "error": True,
            "is_opportunity": False,
            "need_enrichment": False,
            "requisition_type": None,
            "amount": None,
            "amount_basis": "nao_informado",
            "potential_score": 0.0,
            "reason": "; ".join(erros),
            "evidence_quote": "",
        }

    def _normalize(self, result: dict[str, Any], source_text: str | None = None) -> dict[str, Any]:
        required = (
            "is_opportunity",
            "potential_score",
            "reason",
            "evidence_quote",
            "amount",
            "requisition_type",
            "amount_basis",
        )
        if not isinstance(result, dict) or any(chave not in result for chave in required):
            raise ValueError("resposta da IA sem campos obrigatórios")

        normalized = dict(result)
        normalized["is_opportunity"] = _strict_bool(result.get("is_opportunity"))
        normalized["need_enrichment"] = _strict_bool(result.get("need_enrichment"))
        normalized["potential_score"] = _normalize_score(result.get("potential_score"))
        normalized["amount"] = _normalize_amount(result.get("amount"))
        normalized["requisition_type"] = _normalize_requisition_type(result.get("requisition_type"))
        normalized["amount_basis"] = _normalize_amount_basis(result.get("amount_basis"))
        normalized["reason"] = _limpar_texto(result.get("reason"))
        normalized["evidence_quote"] = _limpar_texto(result.get("evidence_quote"))

        if source_text is not None:
            normalized["evidence_quote"] = _repair_evidence_quote(normalized["evidence_quote"], source_text)
            was_positive = normalized["is_opportunity"]
            possui_sinal = _has_credit_signal(source_text)
            evidencia_literal = _evidence_is_literal(normalized["evidence_quote"], source_text)
            if not evidencia_literal:
                normalized["validation_error"] = was_positive
                normalized["is_opportunity"] = False
                normalized["reason"] = self._append_reason(
                    normalized["reason"], "A evidência retornada não é literal no texto-fonte."
                )
            if normalized["amount"] is not None and not _amount_appears_in_text(normalized["amount"], source_text):
                normalized["validation_error"] = was_positive
                normalized["amount"] = None
                normalized["is_opportunity"] = False
                normalized["reason"] = self._append_reason(
                    normalized["reason"], "O valor não foi localizado no texto-fonte."
                )
            if not _basis_is_supported(normalized["amount_basis"], source_text):
                normalized["amount_basis"] = "nao_informado"
            if normalized["amount"] is None:
                normalized["is_opportunity"] = False
                normalized["need_enrichment"] = possui_sinal
                if possui_sinal:
                    normalized["reason"] = self._append_reason(
                        normalized["reason"], "Há sinal de crédito, mas falta valor monetário explícito."
                    )
            elif not possui_sinal:
                normalized["is_opportunity"] = False
                normalized["need_enrichment"] = False
                normalized["reason"] = self._append_reason(
                    normalized["reason"], "O texto não demonstra crédito explicitamente requisitado ou expedido."
                )
        elif normalized["amount"] is None:
            normalized["is_opportunity"] = False

        if normalized["amount"] is None:
            normalized["amount_basis"] = "nao_informado"
        if normalized["is_opportunity"] and normalized["amount"] is None:
            normalized["is_opportunity"] = False
        return normalized

    @staticmethod
    def _append_reason(original: str, complemento: str) -> str:
        if not original:
            return complemento
        if complemento.casefold() in original.casefold():
            return original
        return f"{original} {complemento}".strip()

    async def _request_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        extrator: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        ultimo_erro: _ProviderError | None = None
        async with httpx.AsyncClient(timeout=45) as client:
            for tentativa in range(MAX_RETRIES):
                try:
                    response = await client.post(url, json=payload, headers=headers)
                except httpx.RequestError:
                    ultimo_erro = _ProviderError("erro de rede")
                    if tentativa < MAX_RETRIES - 1:
                        await asyncio.sleep(_retry_delay(None, tentativa))
                        continue
                    raise ultimo_erro

                status = getattr(response, "status_code", 0)
                if status == 429 or 500 <= status <= 599:
                    ultimo_erro = _ProviderError("limite" if status == 429 else "indisponível", status)
                    pedido = _requested_delay(response)
                    if status == 429 and pedido is not None and pedido > MAX_RETRY_DELAY:
                        # Janela de limite longa (ex.: 60s): passa para o próximo provedor já.
                        raise ultimo_erro
                    if tentativa < MAX_RETRIES - 1:
                        await asyncio.sleep(_retry_delay(response, tentativa))
                        continue
                    raise ultimo_erro
                if status == 400:
                    try:
                        code = response.json().get("error", {}).get("code")
                    except (ValueError, TypeError, AttributeError):
                        code = None
                    if code in ("json_validate_failed", "failed_generation") and tentativa < MAX_RETRIES - 1:
                        await asyncio.sleep(_retry_delay(response, tentativa))
                        continue
                if status < 200 or status >= 300:
                    raise _ProviderError("HTTP", status)
                try:
                    corpo = response.json()
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise _ProviderError("resposta JSON inválida") from exc
                return extrator(corpo)
        raise ultimo_erro or _ProviderError("falha após tentativas")

    async def _post(self, url: str, payload: dict, headers: dict) -> dict[str, Any]:
        """POST compatível com APIs OpenAI (Groq e NVIDIA)."""

        return await self._request_json(url, payload, headers, _openai_content)

    async def _post_gemini(self, url: str, payload: dict, headers: dict) -> dict[str, Any]:
        return await self._request_json(url, payload, headers, _gemini_content)

    async def _groq(self, evidence: str) -> dict[str, Any]:
        payload = {
            "model": _setting("groq_model", DEFAULT_GROQ_MODEL),
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": evidence},
            ],
        }
        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return await self._post("https://api.groq.com/openai/v1/chat/completions", payload, headers)

    async def _gemini(self, evidence: str) -> dict[str, Any]:
        modelo = _setting("gemini_model", DEFAULT_GEMINI_MODEL).removeprefix("models/")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(modelo, safe='-_.~')}:{'generateContent'}"
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": evidence}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        headers = {
            "x-goog-api-key": str(settings.gemini_api_key),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return await self._post_gemini(url, payload, headers)

    async def _nvidia(self, evidence: str) -> dict[str, Any]:
        payload = {
            "model": _setting("nvidia_model", DEFAULT_NVIDIA_MODEL),
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": evidence},
            ],
        }
        headers = {
            "Authorization": f"Bearer {settings.nvidia_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return await self._post("https://integrate.api.nvidia.com/v1/chat/completions", payload, headers)
