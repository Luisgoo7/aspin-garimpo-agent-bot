"""Radar ASPIN: newsletter diária por e-mail com notícias do mercado de precatórios e créditos.

Fluxo: coleta (RSS/Atom e páginas públicas) -> filtro por palavras-chave -> IA gratuita
(Gemini, Groq ou NVIDIA) prioriza e resume -> checagem anti-invenção -> HTML -> envio pelo
Apps Script da conta remetente (MailApp). Indicadores vêm direto do Banco Central, sem IA.

Uso: python -m app.newsletter [--dry-run] [--force] [--out arquivo.html]
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import httpx

from .config import settings

log = logging.getLogger(__name__)

TZ = ZoneInfo("America/Sao_Paulo")
USER_AGENT = "RadarASPIN/1.0 (newsletter interna ASPIN Consultoria; 1 leitura/dia)"
STATE_KEY = "newsletter_radar"
MAX_ITENS = 8
MAX_CANDIDATOS = 30
NOTA_MINIMA = 6
LOGO_URL = "https://raw.githubusercontent.com/Luisgoo7/aspin-garimpo-agent-bot/main/docs/assets/aspin_logo_branco.png"

NAVY = "#1F3152"
GOLD = "#B4780A"
GOLD_LIGHT = "#E3B555"
INK = "#1E2633"
MUTED = "#5B6678"


@dataclass(frozen=True)
class Fonte:
    nome: str
    url: str
    tipo: str  # "rss" (RSS ou Atom) ou "cnj" (página pública de notícias)
    oficial: bool
    # Fontes temáticas (tribunais, jornalismo jurídico) passam no filtro com termos amplos;
    # fontes gerais (Câmara, Senado, Agência Brasil) exigem termo específico do mercado.
    tematica: bool = True


FONTES: tuple[Fonte, ...] = (
    Fonte("STF", "https://noticias.stf.jus.br/feed/", "rss", True),
    Fonte("STJ", "https://res.stj.jus.br/hrestp-c-portalp/RSS.xml", "rss", True),
    Fonte("CJF", "https://www.cjf.jus.br/cjf/noticias/RSS", "rss", True),
    Fonte("CNJ", "https://www.cnj.jus.br/noticias/", "cnj", True),
    Fonte("Câmara", "https://www.camara.leg.br/noticias/rss/ultimas-noticias", "rss", True, False),
    Fonte("Senado", "https://www12.senado.leg.br/noticias/rss", "rss", True, False),
    Fonte("Agência Brasil", "https://agenciabrasil.ebc.com.br/rss/economia/feed.xml", "rss", True, False),
    Fonte("Agência Brasil", "https://agenciabrasil.ebc.com.br/rss/justica/feed.xml", "rss", True, False),
    Fonte("JOTA", "https://www.jota.info/feed", "rss", False),
    Fonte("Conjur", "https://www.conjur.com.br/rss.xml", "rss", False),
)

# Termos que sozinhos colocam a notícia no radar (mercado da ASPIN).
TERMOS_FORTES = (
    "precatori", "rpv", "requisicao de pequeno valor", "oficio requisitorio", "requisitorio",
    "cessao de credito", "cessao de precatorio", "credito judicial", "creditos judiciais",
    "fidc", "direitos creditorios", "credito tributario", "creditos tributarios",
    "credito de icms", "creditos de icms", "compensacao tributaria", "transacao tributaria",
    "resolucao 303", "emenda constitucional 113", "emenda constitucional 114", "ec 113", "ec 114",
    "ec 136", "regime especial de pagamento", "divida judicial",
)
# Termos que contam em fontes temáticas (tribunais e jornalismo jurídico).
TERMOS_AMPLOS = (
    "icms", "pis/cofins", "pis e cofins", "restituicao", "repeticao de indebito", "fazenda publica",
    "execucao contra a fazenda", "selic", "ipca", "correcao monetaria", "juros de mora",
    "orcamento", "loa ", "tesouro nacional", "copom", "tributari",
)

SECOES = {
    "tribunais": "Tribunais e CNJ",
    "tributario": "Créditos tributários e ICMS",
    "leis": "Leis, orçamento e pagamentos",
    "mercado": "Mercado, juros e fundos",
}

PRIORIDADES = {
    "alta": ("Alta relevância", NAVY, GOLD_LIGHT),
    "media": ("Média", "#F3E7CF", "#7A5206"),
    "informativo": ("Informativo", "#E7EBF2", NAVY),
}


@dataclass
class Item:
    fonte: str
    titulo: str
    link: str
    publicado: datetime | None
    resumo_oficial: str = ""
    oficial: bool = True
    tematica: bool = True
    # Preenchidos pela curadoria
    nota: int = 0
    secao: str = "tribunais"
    prioridade: str = "informativo"
    resumo: str = ""
    por_que_importa: str = ""
    extras: dict = field(default_factory=dict)

    @property
    def texto_fonte(self) -> str:
        return f"{self.titulo}. {self.resumo_oficial}"


# --------------------------------------------------------------------------- utilidades


def _limpar(valor: Any) -> str:
    if valor is None:
        return ""
    texto = html.unescape(str(valor)).replace(" ", " ")
    texto = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", texto)
    texto = re.sub(r"(?is)<[^>]+>", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _sem_acentos(valor: str) -> str:
    texto = unicodedata.normalize("NFKD", valor.casefold())
    return "".join(c for c in texto if not unicodedata.combining(c))


def _cortar(texto: str, limite: int) -> str:
    texto = texto.strip()
    if len(texto) <= limite:
        return texto
    corte = texto[:limite].rsplit(" ", 1)[0]
    return corte.rstrip(",;:.") + "…"


_MESES_PT = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def parse_data(valor: str | None) -> datetime | None:
    """Datas de RSS (RFC 822), Atom (ISO 8601) e o formato do STJ ("Sex, out 30 2026 16:51:00")."""
    if not valor:
        return None
    valor = valor.strip()
    try:
        data = parsedate_to_datetime(valor)
        return data if data.tzinfo else data.replace(tzinfo=TZ)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        data = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        return data if data.tzinfo else data.replace(tzinfo=TZ)
    except ValueError:
        pass
    m = re.search(r"([a-zç]{3})\w*\s+(\d{1,2})\s+(\d{4})\s+(\d{1,2}):(\d{2})", _sem_acentos(valor))
    if m and m.group(1) in _MESES_PT:
        mes = _MESES_PT[m.group(1)]
        return datetime(int(m.group(3)), mes, int(m.group(2)), int(m.group(4)), int(m.group(5)), tzinfo=TZ)
    return None


def _pascoa(ano: int) -> date:
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(ano, mes, dia)


def feriado_nacional(dia: date) -> bool:
    fixos = {(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25)}
    if (dia.month, dia.day) in fixos:
        return True
    pascoa = _pascoa(dia.year)
    moveis = {pascoa - timedelta(days=48), pascoa - timedelta(days=47), pascoa - timedelta(days=2),
              pascoa + timedelta(days=60)}  # carnaval (seg/ter), sexta santa, Corpus Christi
    return dia in moveis


def dia_de_envio(dia: date) -> bool:
    return dia.weekday() < 5 and not feriado_nacional(dia)


def inicio_da_janela(hoje: date) -> datetime:
    """Desde o último dia de envio: cobre fim de semana e feriados acumulados."""
    anterior = hoje - timedelta(days=1)
    while not dia_de_envio(anterior):
        anterior -= timedelta(days=1)
    return datetime.combine(anterior, datetime.min.time(), tzinfo=TZ).replace(hour=6)


# --------------------------------------------------------------------------- coleta


def parse_feed(conteudo: bytes | str, fonte: Fonte) -> list[Item]:
    try:
        raiz = ET.fromstring(conteudo)
    except ET.ParseError:
        log.warning("feed inválido: %s", fonte.nome)
        return []
    itens: list[Item] = []
    for no in raiz.iter():
        tag = no.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        campos: dict[str, str] = {}
        link = ""
        for filho in no:
            nome = filho.tag.rsplit("}", 1)[-1]
            if nome == "link":
                link = link or (filho.get("href") or filho.text or "").strip()
            elif nome not in campos:
                campos[nome] = filho.text or ""
        titulo = _limpar(campos.get("title"))
        if not titulo or not link.startswith("http"):
            continue
        resumo = _limpar(campos.get("description") or campos.get("summary") or campos.get("content"))
        if _sem_acentos(resumo) == _sem_acentos(titulo):
            resumo = ""
        publicado = parse_data(campos.get("pubDate") or campos.get("published") or campos.get("updated")
                               or campos.get("date"))
        itens.append(Item(fonte.nome, titulo, link, publicado, _cortar(resumo, 700), fonte.oficial, fonte.tematica))
    return itens


def parse_cnj(conteudo: str, base: str) -> list[Item]:
    """Lista de notícias do portal do CNJ (WordPress sem RSS público). Data vem na URL ou no card."""
    itens: list[Item] = []
    vistos: set[str] = set()
    for m in re.finditer(r'<a[^>]+href="(https://www\.cnj\.jus\.br/[a-z0-9\-]{20,}/?)"[^>]*>(.*?)</a>', conteudo, re.S | re.I):
        link, titulo = m.group(1), _limpar(m.group(2))
        if link in vistos or len(titulo) < 25 or link.rstrip("/").endswith("/noticias"):
            continue
        vistos.add(link)
        trecho = conteudo[m.end(): m.end() + 600]
        d = re.search(r"(\d{2})/(\d{2})/(\d{4})", trecho)
        publicado = datetime(int(d.group(3)), int(d.group(2)), int(d.group(1)), 12, tzinfo=TZ) if d else None
        itens.append(Item("CNJ", titulo, urljoin(base, link), publicado, "", True, True))
    return itens


async def coletar(client: httpx.AsyncClient, desde: datetime) -> tuple[list[Item], list[str]]:
    async def uma(fonte: Fonte) -> list[Item]:
        resposta = await client.get(fonte.url)
        resposta.raise_for_status()
        if fonte.tipo == "cnj":
            return parse_cnj(resposta.text, fonte.url)
        return parse_feed(resposta.content, fonte)

    resultados = await asyncio.gather(*(uma(f) for f in FONTES), return_exceptions=True)
    itens: list[Item] = []
    falhas: list[str] = []
    for fonte, resultado in zip(FONTES, resultados):
        if isinstance(resultado, BaseException):
            falhas.append(fonte.nome)
            log.warning("fonte %s falhou: %s", fonte.nome, type(resultado).__name__)
            continue
        for item in resultado:
            # Sem data confiável (página do CNJ) só entra se a página a mostrou entre as recentes.
            if item.publicado is None or item.publicado >= desde:
                itens.append(item)
    return itens, sorted(set(falhas))


def relevancia(item: Item) -> int:
    texto = _sem_acentos(f" {item.titulo} {item.resumo_oficial} ")
    pontos = sum(3 for termo in TERMOS_FORTES if termo in texto)
    if item.tematica:
        pontos += sum(1 for termo in TERMOS_AMPLOS if termo in texto)
    if "precatori" in _sem_acentos(item.titulo):
        pontos += 3
    return pontos


def _chave(titulo: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", _sem_acentos(titulo))[:90]


def filtrar(itens: list[Item], ja_enviados: set[str]) -> list[Item]:
    """Mantém só o que é do mercado, sem repetir edições anteriores; fonte oficial vence duplicata."""
    por_titulo: dict[str, Item] = {}
    for item in itens:
        # 1 ponto basta: a IA dá a nota final. Fontes gerais só pontuam com termo específico.
        if item.link in ja_enviados or relevancia(item) < 1:
            continue
        chave = _chave(item.titulo)
        atual = por_titulo.get(chave)
        if atual is None or (item.oficial and not atual.oficial):
            por_titulo[chave] = item
    ordenados = sorted(por_titulo.values(), key=lambda i: (relevancia(i), i.publicado or datetime.min.replace(tzinfo=TZ)), reverse=True)
    return ordenados[:MAX_CANDIDATOS]


# --------------------------------------------------------------------------- indicadores


SERIES_SGS = (
    ("selic_meta", 432, "Selic meta", "ao ano · Copom"),
    ("selic_mes", 4390, "Selic no mês", "acumulada"),
    ("ipca_mes", 433, "IPCA mês", ""),
    ("ipca_12m", 13522, "IPCA 12 meses", "acumulado"),
)
_MESES_ABREV = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def _fmt_pct(valor: float) -> str:
    return f"{valor:.2f}".replace(".", ",") + "%"


async def indicadores(client: httpx.AsyncClient) -> list[dict]:
    async def serie(codigo: int) -> dict:
        r = await client.get(f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados/ultimos/1?formato=json")
        r.raise_for_status()
        return r.json()[-1]

    saida: list[dict] = []
    resultados = await asyncio.gather(*(serie(c) for _, c, _, _ in SERIES_SGS), return_exceptions=True)
    for (chave, codigo, rotulo, nota), res in zip(SERIES_SGS, resultados):
        if isinstance(res, BaseException):
            continue
        try:
            dia, mes, ano = (int(p) for p in res["data"].split("/"))
            valor = float(res["valor"])
        except (KeyError, ValueError, TypeError):
            continue
        referencia = f"{_MESES_ABREV[mes - 1]}/{ano}"
        detalhe = nota if chave == "selic_meta" else f"{nota} {referencia}".strip()
        saida.append({"chave": chave, "rotulo": rotulo, "valor": _fmt_pct(valor), "nota": detalhe, "serie": codigo})
    try:
        ano = datetime.now(TZ).year
        url = ("https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais"
               f"?$top=1&$filter=Indicador%20eq%20'Selic'%20and%20DataReferencia%20eq%20'{ano}'%20and%20baseCalculo%20eq%200"
               "&$orderby=Data%20desc&$format=json&$select=Data,Mediana")
        r = await client.get(url)
        r.raise_for_status()
        focus = r.json()["value"][0]
        dia_focus = datetime.fromisoformat(focus["Data"]).strftime("%d/%m")
        saida.append({"chave": "focus_selic", "rotulo": f"Focus: Selic fim de {ano}", "valor": _fmt_pct(float(focus["Mediana"])),
                      "nota": f"mediana em {dia_focus}", "serie": "Focus"})
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError):
        log.warning("Focus indisponível")
    return saida


# --------------------------------------------------------------------------- curadoria com IA


PROMPT_CURADORIA = """Você é o editor do Radar ASPIN, boletim diário da ASPIN Consultoria, que intermedeia a venda
de precatórios, RPVs, ofícios requisitórios e créditos tributários entre credores e compradores (fundos, FIDCs).
A leitora é a dona da ASPIN. Ela quer saber o que muda nos negócios dela: regras de cessão, prazos e ordem de
pagamento de precatórios da União, estados e municípios, orçamento para precatórios, emendas constitucionais,
resoluções do CNJ, decisões do STF/STJ sobre créditos tributários (ICMS, PIS/Cofins), juros e correção.

Você recebe uma lista de notícias, cada uma com id, fonte, título e resumo oficial. Regras:
- Use SOMENTE o texto fornecido. Não acrescente números, datas, nomes, valores ou leis que não estejam nele.
- Se o texto não deixa claro o efeito, escreva o "por_que_importa" de forma condicional e curta, sem fatos novos.
- Nota de 0 a 10 = impacto para a ASPIN. Notícias sem ligação com o mercado dela recebem nota até 3.
- secao: "tribunais" (STF, STJ, CNJ, TJs, CJF), "tributario" (teses e créditos tributários), "leis"
  (Congresso, orçamento, Diário Oficial, pagamentos) ou "mercado" (juros, fundos, mercado de crédito).
- prioridade: "alta" (muda regra ou dinheiro do mercado), "media" ou "informativo".
- resumo: 1 ou 2 frases, até 45 palavras, português do Brasil, sem adjetivos de propaganda.
- por_que_importa: 1 frase, até 30 palavras, voltada à ASPIN. Vazio se a nota for menor que 6.
- em_30_segundos: até 3 frases curtas com o mais importante entre as notícias de nota 6 ou mais.
Responda só JSON: {"itens":[{"id":int,"nota":int,"secao":str,"prioridade":str,"resumo":str,
"por_que_importa":str}],"em_30_segundos":[str]}"""


def _entrada_ia(itens: list[Item]) -> str:
    linhas = []
    for i, item in enumerate(itens):
        linhas.append(json.dumps({"id": i, "fonte": item.fonte, "titulo": item.titulo,
                                  "resumo_oficial": _cortar(item.resumo_oficial, 450)}, ensure_ascii=False))
    return "Notícias:\n" + "\n".join(linhas)


async def _chamar_ia(entrada: str) -> tuple[dict, str]:
    """Provedores gratuitos já usados pelo robô; Gemini primeiro pelo contexto maior."""
    from . import llm

    cliente = llm.LLMClassifier()
    tentativas = []
    if settings.gemini_api_key:
        modelo = llm._setting("gemini_model", llm.DEFAULT_GEMINI_MODEL).removeprefix("models/")
        tentativas.append(("Gemini", lambda: cliente._post_gemini(
            f"https://generativelanguage.googleapis.com/v1beta/models/{quote(modelo, safe='-_.~')}:generateContent",
            {"systemInstruction": {"parts": [{"text": PROMPT_CURADORIA}]},
             "contents": [{"role": "user", "parts": [{"text": entrada}]}],
             "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}},
            {"x-goog-api-key": str(settings.gemini_api_key), "Content-Type": "application/json"})))
    for nome, chave, url, ajuste in (
        ("Groq", settings.groq_api_key, "https://api.groq.com/openai/v1/chat/completions", "groq_model"),
        ("NVIDIA", settings.nvidia_api_key, "https://integrate.api.nvidia.com/v1/chat/completions", "nvidia_model"),
    ):
        if not chave:
            continue
        payload = {"model": llm._setting(ajuste, llm.DEFAULT_GROQ_MODEL), "temperature": 0.1,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": PROMPT_CURADORIA}, {"role": "user", "content": entrada}]}
        headers = {"Authorization": f"Bearer {chave}", "Content-Type": "application/json"}
        tentativas.append((nome, lambda u=url, p=payload, h=headers: cliente._post(u, p, h)))
    erros = []
    for nome, chamada in tentativas:
        try:
            resultado = await chamada()
            if isinstance(resultado, dict) and isinstance(resultado.get("itens"), list):
                return resultado, nome
            erros.append(f"{nome}: formato inesperado")
        except Exception as exc:  # noqa: BLE001 - qualquer falha passa ao próximo provedor
            erros.append(f"{nome}: {llm._safe_error(exc)}")
            log.warning("IA %s falhou: %s", nome, llm._safe_error(exc))
    raise RuntimeError("; ".join(erros) or "nenhuma IA configurada")


_NUMERO = re.compile(r"\d+(?:[.,/]\d+)*")


def fatos_conferem(texto: str, fonte: str) -> bool:
    """Todo número escrito pela IA (lei, data, valor, percentual) precisa estar no texto da fonte."""
    def normal(numero: str) -> str:
        return numero.replace(".", "").replace(",", "")

    alvo: set[str] = set()
    for numero in _NUMERO.findall(fonte):
        alvo.add(normal(numero))
        alvo.update(normal(parte) for parte in numero.split("/"))  # "136/2025" vale "136" e "2025"
    return all(normal(numero) in alvo for numero in _NUMERO.findall(texto))


def aplicar_curadoria(itens: list[Item], resposta: dict) -> tuple[list[Item], list[str]]:
    for dado in resposta.get("itens", []):
        try:
            item = itens[int(dado.get("id"))]
            item.nota = max(0, min(10, int(dado.get("nota") or 0)))
        except (TypeError, ValueError, IndexError):
            continue
        secao = str(dado.get("secao") or "").strip().lower()
        item.secao = secao if secao in SECOES else item.secao
        prioridade = _sem_acentos(str(dado.get("prioridade") or "")).strip()
        item.prioridade = prioridade if prioridade in PRIORIDADES else "informativo"
        resumo = _cortar(_limpar(dado.get("resumo")), 400)
        porque = _cortar(_limpar(dado.get("por_que_importa")), 260)
        fonte = item.texto_fonte
        item.resumo = resumo if resumo and fatos_conferem(resumo, fonte) else _cortar(item.resumo_oficial, 300)
        item.por_que_importa = porque if porque and item.nota >= NOTA_MINIMA and fatos_conferem(porque, fonte) else ""
    escolhidos = sorted((i for i in itens if i.nota >= NOTA_MINIMA), key=lambda i: i.nota, reverse=True)[:MAX_ITENS]
    textos_escolhidos = " ".join(i.texto_fonte for i in escolhidos)
    destaques = []
    for frase in resposta.get("em_30_segundos") or []:
        frase = _cortar(_limpar(frase), 220)
        if frase and fatos_conferem(frase, textos_escolhidos):
            destaques.append(frase)
    return escolhidos, destaques[:3]


def curadoria_sem_ia(itens: list[Item]) -> tuple[list[Item], list[str]]:
    """Plano B quando todas as IAs falham: os mais relevantes por palavra-chave, com o texto oficial."""
    escolhidos = []
    for item in [i for i in itens if relevancia(i) >= 2][:MAX_ITENS]:
        item.nota = min(10, 4 + relevancia(item))
        item.prioridade = "media" if relevancia(item) >= 6 else "informativo"
        item.secao = "tributario" if "icms" in _sem_acentos(item.texto_fonte) or "tribut" in _sem_acentos(item.texto_fonte) else (
            "leis" if item.fonte in ("Câmara", "Senado") else "tribunais")
        item.resumo = _cortar(item.resumo_oficial, 300)
        escolhidos.append(item)
    return escolhidos, []


# --------------------------------------------------------------------------- Garimpo (só totais)


def garimpo_de_ontem(hoje: date) -> dict | None:
    if not (settings.database_url or "").strip():
        return None
    try:
        from . import storage

        inicio = datetime.combine(inicio_da_janela(hoje).date(), datetime.min.time(), tzinfo=TZ)
        fim = datetime.combine(hoje, datetime.min.time(), tzinfo=TZ)
        with storage.connect() as conn:
            linha = conn.execute(
                """select count(*) as encontradas,
                          count(*) filter (where decision = 'aprovada') as aprovadas,
                          coalesce(sum(amount) filter (where decision = 'aprovada'), 0) as valor_aprovado
                     from public.opportunities where created_at >= %s and created_at < %s""",
                (inicio, fim),
            ).fetchone()
        return {k: (float(v) if k == "valor_aprovado" else int(v)) for k, v in dict(linha).items()}
    except Exception as exc:  # noqa: BLE001 - seção opcional, nunca derruba a edição
        log.warning("Garimpo indisponível: %s", type(exc).__name__)
        return None


def _fmt_reais(valor: float) -> str:
    if valor >= 1_000_000:
        return "R$ " + f"{valor / 1_000_000:.1f}".replace(".", ",") + " mi"
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


# --------------------------------------------------------------------------- HTML


_DIAS = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
_MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
          "outubro", "novembro", "dezembro"]


def data_extenso(dia: date) -> str:
    return f"{_DIAS[dia.weekday()]}, {dia.day} de {_MESES[dia.month - 1]} de {dia.year}"


def assunto(dia: date, itens: list[Item], inds: list[dict]) -> str:
    base = f"Radar ASPIN | {dia.strftime('%d/%m')}"
    partes = []
    if itens:
        partes.append(_cortar(itens[0].titulo, 70))
    selic = next((i for i in inds if i["chave"] == "selic_meta"), None)
    if selic:
        partes.append(f"Selic em {selic['valor']}")
    return base + (" — " + "; ".join(partes) if partes else "")


def _e(texto: str) -> str:
    return html.escape(texto or "", quote=True)


def _titulo_secao(texto: str) -> str:
    return (f'<tr><td style="padding:22px 32px 8px;"><div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;'
            f'color:{GOLD};font-weight:700;border-left:3px solid {GOLD};padding-left:10px;">{_e(texto)}</div></td></tr>')


def render_html(dia: date, edicao: int, itens: list[Item], destaques: list[str], inds: list[dict],
                garimpo: dict | None, falhas: list[str], remetente: str) -> str:
    fonte = "font-family:Montserrat,Arial,Helvetica,sans-serif;"
    minutos = max(2, round((sum(len(i.resumo) + len(i.por_que_importa) for i in itens) + 600) / 1000) + 1)
    partes: list[str] = []
    partes.append(
        f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Radar ASPIN</title></head><body style="margin:0;padding:0;background:#EEF0F4;">'
        f'<div style="display:none;max-height:0;overflow:hidden;">{_e(f"{len(itens)} notícias selecionadas e os indicadores do Banco Central.")}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#EEF0F4;"><tr><td align="center" style="padding:20px 8px;">'
        f'<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:100%;max-width:640px;background:#FFFFFF;{fonte}color:{INK};">'
        f'<tr><td style="background:{NAVY};padding:22px 32px 18px;">'
        f'<img src="{LOGO_URL}" alt="ASPIN" height="34" style="display:block;height:34px;border:0;">'
        f'<div style="font-family:Orbitron,Montserrat,Arial,sans-serif;color:{GOLD_LIGHT};font-size:12px;letter-spacing:3px;margin-top:12px;">RADAR ASPIN</div>'
        f'<div style="color:#C9D1DE;font-size:12px;margin-top:4px;">{_e(data_extenso(dia))} · Edição nº {edicao:03d} · leitura de {minutos} min</div>'
        f'</td></tr><tr><td style="background:{GOLD};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>'
    )
    if destaques:
        partes.append(_titulo_secao("Em 30 segundos"))
        lis = "".join(f'<li style="margin-bottom:6px;">{_e(d)}</li>' for d in destaques)
        partes.append(f'<tr><td style="padding:0 32px;"><ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.5;">{lis}</ul></td></tr>')
    if inds:
        partes.append(_titulo_secao("Indicadores"))
        celulas = "".join(
            f'<td valign="top" style="width:{100 // min(len(inds), 5)}%;padding:10px;background:#F5F6F9;border:3px solid #FFFFFF;">'
            f'<div style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:{MUTED};">{_e(i["rotulo"])}</div>'
            f'<div style="font-size:19px;font-weight:700;color:{NAVY};margin-top:4px;">{_e(i["valor"])}</div>'
            f'<div style="font-size:10px;color:{MUTED};margin-top:2px;">{_e(i["nota"])}</div></td>'
            for i in inds)
        series = ", ".join(str(i["serie"]) for i in inds if isinstance(i["serie"], int))
        partes.append(
            f'<tr><td style="padding:0 29px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{celulas}</tr></table>'
            f'<div style="font-size:11px;color:{MUTED};margin:6px 3px 0;">Fonte: Banco Central do Brasil (API SGS, séries {series}; Focus). '
            f'Consulta em {dia.strftime("%d/%m/%Y")}.</div></td></tr>')
    for chave, nome in SECOES.items():
        da_secao = [i for i in itens if i.secao == chave]
        if not da_secao and chave == "mercado":
            continue
        partes.append(_titulo_secao(nome))
        if not da_secao:
            partes.append(f'<tr><td style="padding:0 32px;font-size:13.5px;color:#3B4558;">Sem novidades relevantes nas fontes monitoradas.</td></tr>')
            continue
        for n, item in enumerate(da_secao):
            rotulo, fundo, cor = PRIORIDADES.get(item.prioridade, PRIORIDADES["informativo"])
            borda = "" if n == len(da_secao) - 1 else "border-bottom:1px solid #E3E6EC;"
            quando = item.publicado.astimezone(TZ).strftime("%d/%m/%Y") if item.publicado else ""
            porque = (f'<div style="font-size:13px;line-height:1.5;margin-top:6px;color:#3B4558;"><b style="color:{GOLD};">Por que importa:</b> '
                      f'{_e(item.por_que_importa)}</div>') if item.por_que_importa else ""
            resumo = f'<div style="font-size:13.5px;line-height:1.55;">{_e(item.resumo)}</div>' if item.resumo else ""
            partes.append(
                f'<tr><td style="padding:10px 32px 12px;"><div style="{borda}padding-bottom:12px;">'
                f'<span style="display:inline-block;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;'
                f'padding:2px 8px;background:{fundo};color:{cor};">{_e(rotulo)}</span>'
                f'<div style="font-size:15px;font-weight:700;color:{NAVY};margin:6px 0 4px;line-height:1.35;">{_e(item.titulo)}</div>'
                f'{resumo}{porque}'
                f'<div style="font-size:11.5px;color:{MUTED};margin-top:6px;">{_e(item.fonte)}{" · " + quando if quando else ""} · '
                f'<a href="{_e(item.link)}" style="color:{NAVY};">ler na fonte</a></div></div></td></tr>')
    if garimpo is not None:
        partes.append(_titulo_secao("Garimpo do último dia útil"))
        valor = f" · {_fmt_reais(garimpo['valor_aprovado'])} aprovados" if garimpo.get("valor_aprovado") else ""
        partes.append(
            f'<tr><td style="padding:0 32px;"><div style="background:#F5F6F9;border-left:3px solid {NAVY};padding:14px 16px;font-size:13.5px;line-height:1.55;">'
            f'{garimpo["encontradas"]} oportunidades encontradas · {garimpo["aprovadas"]} aprovadas{valor}. Detalhes no CRM ASPIN.'
            f'<div style="font-size:11px;color:{MUTED};">Somente totais; nenhum dado de credor aparece neste e-mail.</div></div></td></tr>')
    aviso_falhas = (f"Fontes fora do ar nesta edição: {', '.join(falhas)}. " if falhas else "")
    descadastro = f"mailto:{remetente}?subject={quote('Cancelar Radar ASPIN')}" if remetente else "#"
    partes.append(
        f'<tr><td style="padding:24px 0 0;"></td></tr><tr><td style="background:{NAVY};color:#C9D1DE;font-size:11px;line-height:1.6;padding:18px 32px;">'
        f'Radar ASPIN · ASPIN Consultoria · CNPJ 22.082.312/0001-24<br>'
        f'Resumos gerados automaticamente a partir de fontes públicas. Confira sempre a fonte antes de decidir. {_e(aviso_falhas)}<br>'
        f'Você recebe este e-mail porque pediu o boletim diário da ASPIN. '
        f'<a href="{_e(descadastro)}" style="color:{GOLD_LIGHT};">Cancelar recebimento</a></td></tr>'
        f'</table></td></tr></table></body></html>')
    return "".join(partes)


def render_texto(dia: date, itens: list[Item], destaques: list[str], inds: list[dict]) -> str:
    linhas = [f"RADAR ASPIN — {data_extenso(dia)}", ""]
    if destaques:
        linhas += ["EM 30 SEGUNDOS"] + [f"- {d}" for d in destaques] + [""]
    if inds:
        linhas += ["INDICADORES"] + [f"- {i['rotulo']}: {i['valor']} ({i['nota']})" for i in inds] + [""]
    for chave, nome in SECOES.items():
        da_secao = [i for i in itens if i.secao == chave]
        if not da_secao:
            continue
        linhas.append(nome.upper())
        for item in da_secao:
            linhas.append(f"- {item.titulo} ({item.fonte})")
            if item.resumo:
                linhas.append(f"  {item.resumo}")
            if item.por_que_importa:
                linhas.append(f"  Por que importa: {item.por_que_importa}")
            linhas.append(f"  {item.link}")
        linhas.append("")
    linhas.append("Resumos automáticos a partir de fontes públicas; confira a fonte antes de decidir.")
    return "\n".join(linhas)


# --------------------------------------------------------------------------- envio


def destinatarios() -> list[str]:
    bruto = os.environ.get("NEWSLETTER_TO", "")
    return [e.strip() for e in re.split(r"[,;\s]+", bruto) if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e.strip())]


async def enviar(client: httpx.AsyncClient, para: list[str], titulo: str, corpo_html: str, corpo_texto: str) -> None:
    """Envia pelo Web App do Apps Script da conta remetente (MailApp). Nenhuma senha sai da conta Google."""
    url = os.environ.get("NEWSLETTER_MAILER_URL", "").strip()
    token = os.environ.get("NEWSLETTER_MAILER_TOKEN", "").strip()
    if not url or not token:
        raise RuntimeError("envio não configurado (NEWSLETTER_MAILER_URL/NEWSLETTER_MAILER_TOKEN)")
    payload = {"token": token, "action": "send", "to": para, "subject": titulo, "html": corpo_html, "text": corpo_texto,
               "name": "Radar ASPIN"}
    resposta = await client.post(url, content=json.dumps(payload), headers={"Content-Type": "text/plain;charset=utf-8"},
                                 follow_redirects=True, timeout=60)
    try:
        dado = resposta.json()
    except ValueError:
        raise RuntimeError(f"envio: resposta inesperada (HTTP {resposta.status_code})") from None
    if not dado.get("ok"):
        raise RuntimeError(f"envio recusado: {str(dado.get('error') or 'sem detalhe')[:120]}")


async def avisar_telegram(client: httpx.AsyncClient, texto: str) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    try:
        await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                          json={"chat_id": settings.telegram_chat_id, "text": texto})
    except httpx.HTTPError:
        pass


# --------------------------------------------------------------------------- estado


def ler_estado() -> dict:
    if not (settings.database_url or "").strip():
        return {}
    from . import storage

    return storage.get_state(STATE_KEY) or {}


def gravar_estado(estado: dict) -> None:
    if not (settings.database_url or "").strip():
        return
    from . import storage

    storage.set_state(STATE_KEY, estado)


# --------------------------------------------------------------------------- execução


async def montar_edicao(hoje: date, estado: dict) -> dict:
    ja_enviados = set(estado.get("links", []))
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        (brutos, falhas), inds = await asyncio.gather(coletar(client, inicio_da_janela(hoje)), indicadores(client))
    candidatos = filtrar(brutos, ja_enviados)
    log.info("coletados=%d candidatos=%d falhas=%s", len(brutos), len(candidatos), falhas)
    ia = "sem itens"
    destaques: list[str] = []
    itens: list[Item] = []
    if candidatos:
        try:
            resposta, ia = await _chamar_ia(_entrada_ia(candidatos))
            itens, destaques = aplicar_curadoria(candidatos, resposta)
        except RuntimeError as exc:
            log.error("curadoria com IA falhou, usando palavras-chave: %s", exc)
            ia = "palavras-chave (IAs indisponíveis)"
            itens, destaques = curadoria_sem_ia(candidatos)
    remetente = os.environ.get("NEWSLETTER_FROM", "aspininforma@gmail.com")
    edicao = int(estado.get("edicao", 0)) + 1
    garimpo = garimpo_de_ontem(hoje) if os.environ.get("NEWSLETTER_GARIMPO", "true").lower() != "false" else None
    return {
        "assunto": assunto(hoje, itens, inds),
        "html": render_html(hoje, edicao, itens, destaques, inds, garimpo, falhas, remetente),
        "texto": render_texto(hoje, itens, destaques, inds),
        "itens": itens, "falhas": falhas, "ia": ia, "edicao": edicao, "candidatos": len(candidatos),
    }


async def executar(dry_run: bool = False, forcar: bool = False, saida: str | None = None) -> int:
    hoje = datetime.now(TZ).date()
    if not forcar and not dia_de_envio(hoje):
        log.info("hoje (%s) não é dia útil: sem edição", hoje)
        return 0
    estado = ler_estado()
    if not forcar and not dry_run and estado.get("ultimo_envio") == hoje.isoformat():
        log.info("edição de hoje já enviada")
        return 0
    edicao = await montar_edicao(hoje, estado)
    if saida:
        with open(saida, "w", encoding="utf-8") as arquivo:
            arquivo.write(edicao["html"])
    print(f"Assunto: {edicao['assunto']}")
    print(f"Itens: {len(edicao['itens'])} de {edicao['candidatos']} candidatos · curadoria: {edicao['ia']} · "
          f"fontes com falha: {', '.join(edicao['falhas']) or 'nenhuma'}")
    if dry_run:
        return 0
    para = destinatarios()
    async with httpx.AsyncClient(timeout=60) as client:
        if not para:
            await avisar_telegram(client, "Radar ASPIN: nenhum destinatário configurado (NEWSLETTER_TO).")
            raise SystemExit("NEWSLETTER_TO vazio")
        try:
            await enviar(client, para, edicao["assunto"], edicao["html"], edicao["texto"])
        except Exception as exc:
            await avisar_telegram(client, f"Radar ASPIN: falha no envio de hoje ({str(exc)[:150]}). Nova tentativa no próximo agendamento.")
            raise
    links = [i.link for i in edicao["itens"]] + list(estado.get("links", []))
    gravar_estado({"ultimo_envio": hoje.isoformat(), "edicao": edicao["edicao"], "links": links[:400],
                   "ultima_ia": edicao["ia"], "itens": len(edicao["itens"])})
    print(f"Enviado para {len(para)} destinatário(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Radar ASPIN — newsletter diária")
    parser.add_argument("--dry-run", action="store_true", help="monta a edição sem enviar")
    parser.add_argument("--force", action="store_true", help="envia mesmo fora de dia útil ou se já enviou hoje")
    parser.add_argument("--out", help="grava o HTML da edição neste arquivo")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(settings.log_level).upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(executar(args.dry_run, args.force, args.out))


if __name__ == "__main__":
    sys.exit(main())
