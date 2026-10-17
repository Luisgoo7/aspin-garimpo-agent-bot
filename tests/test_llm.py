import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import llm


def resposta_base(**alteracoes):
    resposta = {
        "is_opportunity": True,
        "need_enrichment": False,
        "requisition_type": "rpv",
        "amount": "R$ 1.234,56",
        "amount_basis": "nao_informado",
        "potential_score": 80,
        "reason": "RPV expedida com valor explícito.",
        "evidence_quote": "RPV expedida no valor de R$ 1.234,56",
        "beneficiary": None,
        "debtor_entity": None,
        "lawyer": None,
        "issued_at": None,
        "deadline_at": None,
    }
    resposta.update(alteracoes)
    return resposta


class RespostaFake:
    def __init__(self, status_code, corpo, headers=None):
        self.status_code = status_code
        self._corpo = corpo
        self.headers = headers or {}

    def json(self):
        return self._corpo


class ClienteFake:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, json, headers):
        self.chamadas.append((url, json, headers))
        resposta = self.respostas.pop(0)
        if isinstance(resposta, BaseException):
            raise resposta
        return resposta


def test_normalizacao_strict_bool_score_amount_e_basis():
    classifier = llm.LLMClassifier()
    texto = "RPV expedida no valor de R$ 1.234,56; simples menção a valor líquido."
    resultado = classifier._normalize(
        resposta_base(is_opportunity="false", potential_score="80%", amount_basis="liquido"),
        texto,
    )

    assert resultado["is_opportunity"] is False
    assert resultado["potential_score"] == pytest.approx(0.8)
    assert resultado["amount"] == pytest.approx(1234.56)
    assert resultado["amount_basis"] == "liquido"

    sem_indicacao = classifier._normalize(
        resposta_base(amount_basis="liquido"),
        "RPV expedida no valor de R$ 1.234,56.",
    )
    assert sem_indicacao["amount_basis"] == "nao_informado"


def test_evidencia_literal_normaliza_html_e_espacos():
    classifier = llm.LLMClassifier()
    fonte = "<p>RPV   expedida no valor de <strong>R$ 1.234,56</strong>.</p>"
    resultado = classifier._normalize(
        resposta_base(evidence_quote="RPV expedida no valor de R$ 1.234,56"),
        fonte,
    )
    assert resultado["is_opportunity"] is True
    assert llm._evidence_is_literal("RPV expedida   no valor de R$ 1.234,56", fonte)

    rejeitada = classifier._normalize(
        resposta_base(evidence_quote="Precatório expedido no valor de R$ 1.234,56"),
        fonte,
    )
    assert rejeitada["is_opportunity"] is False


def test_credito_sem_valor_rejeita_mas_marca_enriquecimento():
    classifier = llm.LLMClassifier()
    fonte = "Ofício requisitório expedido para pagamento ao beneficiário."
    resultado = classifier._normalize(
        resposta_base(amount=None, is_opportunity=True, evidence_quote="Ofício requisitório expedido"),
        fonte,
    )
    assert resultado["is_opportunity"] is False
    assert resultado["need_enrichment"] is True
    assert resultado["amount"] is None


def test_json_fence_robusto():
    corpo = llm._decode_json_content('prefixo\n```JSON\n{"ok": true}\n```\nsufixo')
    assert corpo == {"ok": True}


def test_fallback_groq_gemini_nvidia(monkeypatch):
    monkeypatch.setattr(llm.settings, "groq_api_key", "groq", raising=False)
    monkeypatch.setattr(llm.settings, "gemini_api_key", "gemini", raising=False)
    monkeypatch.setattr(llm.settings, "nvidia_api_key", "nvidia", raising=False)
    classifier = llm.LLMClassifier()
    classifier._groq = AsyncMock(side_effect=RuntimeError("indisponível"))
    classifier._gemini = AsyncMock(side_effect=RuntimeError("indisponível"))
    classifier._nvidia = AsyncMock(return_value=resposta_base())

    resultado = asyncio.run(classifier.classify("RPV expedida no valor de R$ 1.234,56"))

    assert resultado["is_opportunity"] is True
    classifier._groq.assert_awaited_once()
    classifier._gemini.assert_awaited_once()
    classifier._nvidia.assert_awaited_once()


def test_gemini_usa_endpoint_nativo_e_cabecalho(monkeypatch):
    monkeypatch.setattr(llm.settings, "gemini_api_key", "chave-gemini", raising=False)
    monkeypatch.setattr(llm.settings, "gemini_model", "gemini-teste", raising=False)
    classifier = llm.LLMClassifier()
    classifier._post_gemini = AsyncMock(return_value=resposta_base())

    resultado = asyncio.run(classifier._gemini("texto da publicação"))

    assert resultado["requisition_type"] == "rpv"
    classifier._post_gemini.assert_awaited_once()
    url, payload, headers = classifier._post_gemini.await_args.args
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-teste:generateContent"
    assert headers["x-goog-api-key"] == "chave-gemini"
    assert payload["systemInstruction"]["parts"][0]["text"] == llm.SYSTEM_PROMPT
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "texto da publicação"}]}]
    assert payload["generationConfig"]["responseMimeType"] == "application/json"


def test_retries_limitados_em_429_5xx_e_respeitam_retry_after(monkeypatch):
    respostas = [
        RespostaFake(429, {}, {"retry-after": "0"}),
        RespostaFake(503, {}, {"retry-after": "0"}),
        RespostaFake(
            200,
            {"choices": [{"message": {"content": json.dumps(resposta_base())}}]},
        ),
    ]
    cliente = ClienteFake(respostas)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda timeout: cliente)
    monkeypatch.setattr(llm.asyncio, "sleep", AsyncMock())

    resultado = asyncio.run(
        llm.LLMClassifier()._post(
            "https://api.groq.com/openai/v1/chat/completions",
            {},
            {},
        )
    )

    assert resultado["amount"] == "R$ 1.234,56"
    assert len(cliente.chamadas) == 3
    assert llm.asyncio.sleep.await_count == 2


def test_falha_http_nao_expoe_url_ou_segredo(monkeypatch, caplog):
    respostas = [RespostaFake(401, {})] * 3
    cliente = ClienteFake(respostas)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda timeout: cliente)
    classifier = llm.LLMClassifier()
    monkeypatch.setattr(llm.settings, "groq_api_key", "segredo", raising=False)
    monkeypatch.setattr(llm.settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(llm.settings, "nvidia_api_key", "", raising=False)

    with caplog.at_level("ERROR"):
        resultado = asyncio.run(classifier.classify("texto"))

    assert resultado["error"] is True
    assert "segredo" not in caplog.text
    assert "api.groq.com" not in caplog.text
    assert "HTTP 401" in caplog.text
    assert len(cliente.chamadas) == 1


def test_valor_inventado_usa_fallback_em_vez_de_rejeicao_definitiva(monkeypatch):
    monkeypatch.setattr(llm.settings, 'groq_api_key', 'groq')
    monkeypatch.setattr(llm.settings, 'gemini_api_key', 'gemini')
    monkeypatch.setattr(llm.settings, 'nvidia_api_key', '')
    classifier = llm.LLMClassifier()
    classifier._groq = AsyncMock(return_value=resposta_base(amount=2000))
    classifier._gemini = AsyncMock(return_value=resposta_base(amount=1234.56))
    result = asyncio.run(classifier.classify('RPV expedida no valor de R$ 1.234,56'))
    assert result['is_opportunity'] is True
    assert result['amount'] == 1234.56
    classifier._gemini.assert_awaited_once()


def test_citacao_abreviada_so_reconstroi_trechos_reais():
    source = 'Homologo o valor de R$ 135.654,57 (principal) e honorários separados. Requisitem-se os valores.'
    quote = 'Homologo o valor de R$ 135.654,57 (principal) ... Requisitem-se os valores.'
    repaired = llm._repair_evidence_quote(quote, source)
    assert repaired == source
    invented = 'Homologo o valor de R$ 999.999,99 ... Requisitem-se os valores.'
    assert llm._repair_evidence_quote(invented, source) == invented


def test_publicacao_com_principal_e_honorarios_nao_soma():
    text = 'Homologo o valor de R$ 135.654,57 (principal) e de R$ 13.565,44 (honorários). Requisitem-se os valores. Dê-se vista do ofício requisitório.'
    result = llm.LLMClassifier()._normalize(resposta_base(
        amount=135654.57, amount_basis='principal', requisition_type='oficio_requisitorio',
        evidence_quote='Homologo o valor de R$ 135.654,57 (principal) ... Requisitem-se os valores.'), text)
    assert result['is_opportunity'] is True
    assert result['amount'] == 135654.57
    assert result['evidence_quote'] in text


def test_json_generation_rejection_retries(monkeypatch):
    respostas=[RespostaFake(400,{'error':{'code':'json_validate_failed'}}),RespostaFake(200,{'choices':[{'message':{'content':json.dumps(resposta_base())}}]})]
    cliente=ClienteFake(respostas)
    monkeypatch.setattr(llm.httpx,'AsyncClient',lambda timeout:cliente)
    monkeypatch.setattr(llm.asyncio,'sleep',AsyncMock())
    result=asyncio.run(llm.LLMClassifier()._post('https://api.groq.com/openai/v1/chat/completions',{},{}))
    assert result['amount']=='R$ 1.234,56'
    assert len(cliente.chamadas)==2
