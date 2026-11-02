import json
from datetime import date, datetime

import httpx
import pytest

from app import newsletter as nl

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>
<item><title><![CDATA[CNJ apresenta regra para cess\xc3\xa3o de precat\xc3\xb3rio]]></title>
<link>https://exemplo.jus.br/a</link><pubDate>Mon, 02 Nov 2026 10:02:51 +0000</pubDate>
<description><![CDATA[<p>Minuta da Resolu\xc3\xa7\xc3\xa3o 303 ainda ser\xc3\xa1 votada em 2026.</p>]]></description></item>
<item><title>Campeonato de futebol</title><link>https://exemplo.jus.br/b</link>
<pubDate>Mon, 02 Nov 2026 11:00:00 +0000</pubDate><description>Nada a ver</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>CJF</title>
<entry><title>CJF libera RPVs de outubro</title><link href="https://www.cjf.jus.br/x"/>
<updated>2026-11-02T09:00:00-03:00</updated><summary>Libera\xc3\xa7\xc3\xa3o de requisi\xc3\xa7\xc3\xb5es de pequeno valor (RPV).</summary></entry></feed>"""


def test_parse_data_formatos():
    assert nl.parse_data("Mon, 02 Nov 2026 10:02:51 +0000").year == 2026
    stj = nl.parse_data("Sex, out 30 2026 16:51:00")
    assert (stj.month, stj.day, stj.hour) == (10, 30, 16)
    assert nl.parse_data("2026-11-02T09:00:00-03:00").day == 2
    assert nl.parse_data("ontem") is None


def test_feriados_e_janela():
    assert nl.feriado_nacional(date(2026, 11, 2))  # Finados
    assert nl.feriado_nacional(date(2026, 4, 3))  # Sexta-feira Santa (Páscoa 05/04/2026)
    assert nl.feriado_nacional(date(2026, 2, 17))  # terça de Carnaval
    assert not nl.dia_de_envio(date(2026, 11, 7))  # sábado
    assert nl.dia_de_envio(date(2026, 11, 3))
    # Terça depois de feriado na segunda: janela volta até sexta.
    assert nl.inicio_da_janela(date(2026, 11, 3)).date() == date(2026, 10, 30)


def test_parse_feed_rss_e_atom():
    rss = nl.parse_feed(RSS, nl.Fonte("JOTA", "u", "rss", False))
    assert [i.link for i in rss] == ["https://exemplo.jus.br/a", "https://exemplo.jus.br/b"]
    assert "Resolução 303" in rss[0].resumo_oficial and "<p>" not in rss[0].resumo_oficial
    atom = nl.parse_feed(ATOM, nl.Fonte("CJF", "u", "rss", True))
    assert atom[0].link == "https://www.cjf.jus.br/x" and atom[0].publicado.day == 2


def test_filtro_mantem_mercado_e_prefere_fonte_oficial():
    jota = nl.parse_feed(RSS, nl.Fonte("JOTA", "u", "rss", False))
    oficial = nl.Item("CNJ", jota[0].titulo, "https://cnj.jus.br/a", None, "", True)
    escolhidos = nl.filtrar(jota + [oficial], ja_enviados=set())
    assert [i.fonte for i in escolhidos] == ["CNJ"]  # duplicata vira uma; futebol sai
    assert nl.filtrar(jota, ja_enviados={"https://exemplo.jus.br/a"}) == []


def test_fonte_geral_exige_termo_especifico():
    geral = nl.Item("Senado", "Senado aprova orçamento da saúde", "https://s/1", None, "Texto sobre ICMS e Selic.", True, False)
    assert nl.relevancia(geral) < 2


def test_checagem_anti_invencao():
    fonte = "STF julga a EC 136/2025 e fixa prazo de 30 dias. Selic de 13,75%."
    assert nl.fatos_conferem("O STF julgou a EC 136/2025 com prazo de 30 dias.", fonte)
    assert nl.fatos_conferem("Selic em 13,75%.", fonte)
    assert not nl.fatos_conferem("Prazo de 60 dias para pagamento.", fonte)
    assert not nl.fatos_conferem("Impacto de R$ 2 bilhões.", fonte)


def test_curadoria_descarta_resumo_com_numero_inventado():
    itens = [nl.Item("STJ", "STJ julga cessão de precatório", "https://stj/1", None, "Audiência pública sobre cessão.", True),
             nl.Item("JOTA", "Notícia fraca sobre precatório", "https://j/2", None, "Texto.", False)]
    resposta = {"itens": [
        {"id": 0, "nota": 9, "secao": "tribunais", "prioridade": "alta", "resumo": "O STJ decidiu em 2027 que 40% pode ser cedido.",
         "por_que_importa": "Pode mudar quem compra esse crédito."},
        {"id": 1, "nota": 3, "secao": "tribunais", "prioridade": "informativo", "resumo": "x", "por_que_importa": "y"},
    ], "em_30_segundos": ["STJ discute cessão de precatório.", "Valor de R$ 9 bi em jogo."]}
    escolhidos, destaques = nl.aplicar_curadoria(itens, resposta)
    assert [i.link for i in escolhidos] == ["https://stj/1"]
    assert escolhidos[0].resumo == "Audiência pública sobre cessão."  # voltou ao texto oficial
    assert escolhidos[0].por_que_importa == "Pode mudar quem compra esse crédito."
    assert destaques == ["STJ discute cessão de precatório."]


def test_html_escapa_e_tem_descadastro():
    item = nl.Item("STJ", "Título <b>perigoso</b>", "https://stj/1?a=1&b=2", datetime(2026, 11, 2, tzinfo=nl.TZ), "", True)
    item.resumo, item.prioridade, item.secao = "Resumo & detalhe", "alta", "tribunais"
    corpo = nl.render_html(date(2026, 11, 3), 1, [item], ["Destaque"], [], None, ["CNJ"], "aspininforma@gmail.com")
    assert "&lt;b&gt;perigoso" in corpo and "Resumo &amp; detalhe" in corpo
    assert "mailto:aspininforma@gmail.com" in corpo and "Fontes fora do ar nesta edição: CNJ" in corpo
    assert "Sem novidades relevantes" in corpo  # seções vazias aparecem
    assert "Edição nº 001" in corpo


def test_destinatarios(monkeypatch):
    monkeypatch.setenv("NEWSLETTER_TO", "a@b.com; invalido, c@d.com.br")
    assert nl.destinatarios() == ["a@b.com", "c@d.com.br"]


@pytest.mark.asyncio
async def test_enviar_usa_apps_script(monkeypatch):
    monkeypatch.setenv("NEWSLETTER_MAILER_URL", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setenv("NEWSLETTER_MAILER_TOKEN", "segredo")
    recebidos = []

    def responder(request):
        recebidos.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        await nl.enviar(client, ["a@b.com"], "Assunto", "<p>x</p>", "x")
    assert recebidos[0]["token"] == "segredo" and recebidos[0]["to"] == ["a@b.com"]

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": False, "error": "token inválido"}))) as client:
        with pytest.raises(RuntimeError, match="token inválido"):
            await nl.enviar(client, ["a@b.com"], "A", "h", "t")
