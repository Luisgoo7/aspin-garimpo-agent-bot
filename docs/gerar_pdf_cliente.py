"""Gera docs/ASPIN_Garimpo_como_funciona.pdf (1 página, linguagem para a cliente)."""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).with_name("ASPIN_Garimpo_como_funciona.pdf")
NAVY = colors.HexColor("#0B3D5C")
TEAL = colors.HexColor("#1B7F8C")
LIGHT = colors.HexColor("#EEF4F7")
GREEN = colors.HexColor("#C8E6C9")
YELLOW = colors.HexColor("#FFF3C4")
RED = colors.HexColor("#FAD4D4")
GREY = colors.HexColor("#5F6B73")

pdfmetrics.registerFont(TTFont("Body", r"C:\Windows\Fonts\segoeui.ttf"))
pdfmetrics.registerFont(TTFont("Bold", r"C:\Windows\Fonts\segoeuib.ttf"))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="Bold", italic="Body", boldItalic="Bold")

base = ParagraphStyle("base", fontName="Body", fontSize=8.9, leading=11.6, textColor=colors.HexColor("#1F2A30"))
small = ParagraphStyle("small", parent=base, fontSize=7.8, leading=10)
h2 = ParagraphStyle("h2", parent=base, fontName="Bold", fontSize=11.5, leading=14, textColor=NAVY, spaceAfter=3)
cell = ParagraphStyle("cell", parent=base, fontSize=8.5, leading=10.8)
cell_b = ParagraphStyle("cellb", parent=cell, fontName="Bold")
cell_c = ParagraphStyle("cellc", parent=cell_b, alignment=TA_CENTER)
white_b = ParagraphStyle("whiteb", parent=cell_b, textColor=colors.white)
white_c = ParagraphStyle("whitec", parent=white_b, alignment=TA_CENTER)


def P(text, style=base):
    return Paragraph(text, style)


def header():
    title = Paragraph('<font name="Bold" size="18" color="white">Aspin Garimpo</font>'
                      '<font size="11.5" color="#CFE3EC">  ·  como funciona o robô de oportunidades</font>',
                      ParagraphStyle("title", parent=base, leading=24))
    sub = Paragraph('<font color="#CFE3EC" size="9">Monitoramento automático de precatórios e RPVs em fontes '
                    'públicas oficiais, com entrega das melhores oportunidades para análise da equipe.</font>', base)
    t = Table([[title], [sub]], colWidths=[18.2 * cm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY),
                           ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                           ("TOPPADDING", (0, 0), (0, 0), 12), ("BOTTOMPADDING", (0, 0), (0, 0), 2),
                           ("BOTTOMPADDING", (0, -1), (-1, -1), 12)]))
    return t


def resumo():
    t = Table([[P("<b>Em uma frase:</b> o robô lê, de hora em hora, as publicações oficiais dos tribunais, "
                  "identifica ofícios requisitórios, precatórios e RPVs com valor, confere os dados e entrega "
                  "<b>as 3 melhores oportunidades às 08h, 12h e 18h</b> no Telegram. A equipe aprova ou descarta "
                  "com um toque; o que é aprovado vai automaticamente para a planilha de trabalho.")]],
              colWidths=[18.2 * cm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("LINEBEFORE", (0, 0), (0, -1), 3, TEAL),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 7),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return t


def etapas():
    steps = [
        ("1  Coleta", "A cada hora, lê o <b>DJEN</b> (Diário de Justiça Eletrônico Nacional, do CNJ) buscando termos como "
                      "“ofício requisitório”, “expedição de precatório” e “requisição de pagamento”. Também consulta o "
                      "<b>DataJud</b> (base oficial do CNJ) pelo movimento “Expedição de precatório/RPV” em <b>todos os 27 Tribunais de Justiça e nos 6 TRFs</b>."),
        ("2  Triagem", "Um filtro automático descarta o que não traz valor escrito ou fica abaixo de "
                       "<b>R$ 100 mil</b>. Só o que sobra segue para a leitura."),
        ("3  Leitura", "Uma inteligência artificial lê o texto e extrai: tipo de requisição, valor, credor, ente devedor, "
                       "natureza, preferência, honorários destacados, trânsito em julgado e terceiros interessados. "
                       "<b>Trava de segurança:</b> o valor e o trecho precisam estar literalmente na publicação; a IA não pode inventar dados."),
        ("4  Entrega", "Às <b>08h, 12h e 18h</b> chegam as 3 melhores no Telegram, com a Nota ASPIN e o link da publicação. "
                       "<b>Aprovar</b> grava tudo na planilha em segundos; <b>Descartar</b> registra a decisão. Consulta rápida: <b>/processo</b> + número traz o resumo de qualquer processo."),
    ]
    row_t = [P(f'<font name="Bold" color="white">{t}</font>', base) for t, _ in steps]
    row_b = [P(b, cell) for _, b in steps]
    w = 18.2 * cm / 4
    t = Table([row_t, row_b], colWidths=[w] * 4)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), TEAL), ("BACKGROUND", (0, 1), (-1, 1), colors.white),
                           ("BOX", (0, 0), (-1, -1), 0.6, TEAL), ("INNERGRID", (0, 0), (-1, -1), 0.6, TEAL),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 6),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 5),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def nota():
    rows = [[P("Critério conferido", white_b), P("Pontos", white_c), P("Por que importa", white_b)]]
    data = [
        ("Valor citado no texto da publicação", "2", "O valor está escrito na própria decisão ou ofício; não é estimativa do robô."),
        ("Requisição expedida ou ordenada", "2", "O juízo já determinou a expedição do ofício requisitório, precatório ou RPV: fase avançada."),
        ("Expedição confirmada no DataJud (CNJ)", "2", "O movimento “Expedição de precatório/RPV” aparece também na base oficial do CNJ."),
        ("Sem indício de cessão", "2", "Não há FIDC, banco ou cessionário como terceiro interessado; o crédito tende a estar com o titular."),
        ("Natureza alimentar", "1", "Créditos alimentares têm ordem de pagamento própria (art. 100, § 1º, CF)."),
        ("Preferência legal", "1", "Titular idoso, com doença grave ou com deficiência recebe com preferência (art. 100, § 2º, CF)."),
    ]
    for crit, pts, why in data:
        rows.append([P(crit, cell_b), P(pts, cell_c), P(why, cell)])
    rows.append([P("Total", white_b), P("10", white_c),
                 P('<font color="white"><b>8 a 10</b> alta prioridade  ·  <b>5 a 7</b> média  ·  <b>0 a 4</b> baixa</font>', cell)])
    t = Table(rows, colWidths=[5.6 * cm, 1.6 * cm, 11.0 * cm])
    style = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("BACKGROUND", (0, -1), (-1, -1), NAVY),
             ("BOX", (0, 0), (-1, -1), 0.6, NAVY), ("LINEBELOW", (0, 1), (-1, -2), 0.3, colors.HexColor("#C9D6DD")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 6),
             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    for i in range(1, len(rows) - 1):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), LIGHT))
    t.setStyle(TableStyle(style))
    return t


def rodape_colunas():
    escolha = [P("Como são escolhidas as 3 melhores", h2),
               P("Entre as oportunidades que passaram pelas travas, o robô prioriza o <b>maior valor</b>, com bônus "
                 "para natureza alimentar, preferência legal e confirmação no DataJud, e <b>forte penalidade</b> "
                 "quando há indício de cessão. As demais ficam registradas no banco, mas não são enviadas, "
                 "para não poluir o canal.", base),
               Spacer(1, 4),
               P("Na mensagem do Telegram aparecem: valor, tipo, tribunal, processo, credor e devedor, a Nota ASPIN e o "
                 "link da publicação. Na planilha, ao aprovar, entram todos os detalhes (nº da requisição, classe, "
                 "órgão julgador, honorários, trânsito em julgado, trecho e fonte).", base)]
    limites = [P("O que o robô não faz", h2),
               P("• <b>Não substitui a análise jurídica.</b> Antes de abordar o credor, conferir titularidade, "
                 "cessões, penhoras e compensações, habilitação de herdeiros e a situação atual no tribunal.", base),
               P("• <b>O valor é o citado na publicação.</b> Pode ser bruto ou de data-base anterior; atualização, IR, "
                 "PSS e honorários entram na análise.", base),
               P("• <b>Só usa fontes públicas oficiais.</b> Não acessa processos em segredo de justiça nem contorna "
                 "captcha de sistemas como o e-SAJ.", base),
               P("• <b>A comissão exibida é uma estimativa</b>, não um resultado garantido.", base)]
    t = Table([[escolha, limites]], colWidths=[9.0 * cm, 9.2 * cm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (0, 0), 12), ("LINEBEFORE", (1, 0), (1, 0), 0.6, TEAL),
                           ("LEFTPADDING", (1, 0), (1, 0), 12)]))
    return t


def build():
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=1.4 * cm, rightMargin=1.4 * cm,
                            topMargin=1.2 * cm, bottomMargin=1.0 * cm,
                            title="Aspin Garimpo — como funciona", author="ASPIN")
    story = [header(), Spacer(1, 8), resumo(), Spacer(1, 9),
             P("Como funciona, em 4 etapas", h2), etapas(), Spacer(1, 9),
             P("Nota ASPIN: de 0 a 10, só com critérios que podem ser conferidos", h2),
             P("Cada oportunidade recebe uma nota pela soma dos critérios abaixo. Todos podem ser verificados na "
               "publicação ou no DataJud; a nota não é uma opinião do robô.", small),
             Spacer(1, 4), nota(), Spacer(1, 10), rodape_colunas(), Spacer(1, 8),
             P('<font color="#5F6B73">Fontes: DJEN/Comunica PJe e DataJud, ambos do Conselho Nacional de Justiça. '
               'Coleta de hora em hora; entregas às 08h, 12h e 18h (horário de Brasília). Ferramenta de apoio à '
               'prospecção: cada caso exige validação jurídica e documental pela equipe ASPIN.</font>', small)]
    doc.build(story)
    return OUT


if __name__ == "__main__":
    print(build())
