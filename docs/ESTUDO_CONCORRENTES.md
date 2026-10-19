# Estudo de plataformas pagas de precatórios (somente informação pública)

Levantamento feito em 19/10/2026 a partir das páginas públicas de cada produto
(sites, planos e FAQs). Nenhuma conta foi criada, nenhum trial foi usado e nenhum
dado interno foi acessado. O que está marcado como **inferência** é dedução a
partir do que a página diz, não informação confirmada pela empresa.

## Resumo comparativo

| Plataforma | Modelo | Preço público | Fonte de dados (declarada / inferida) | Qualificação de lead |
|---|---|---|---|---|
| **JUDIT Miner** (judit.io) | Base pesquisável + exportação | R$ 3.750/mês (trimestral); R$ 2.437,50 (semestral); R$ 1.875 (anual). 250–750 exportações/mês, 1–3 assentos | "+1 milhão de precatórios", federal/estadual/trabalhista, **LOAs 2024–2028**; pré-precatórios = processos em cumprimento de sentença (235 mil+ na LOA 2027). **Inferência:** listas/LOAs publicadas pelos tribunais + base processual própria (a JUDIT vende API de consulta processual) | Filtros: tribunal, UF, valor, beneficiário, ente devedor, fase processual, LOA, partes. "Enriquecimento" de contato do beneficiário conforme plano |
| **JURISCAN** (juriscan.com.br) | SaaS + curadoria de lotes | Essencial R$ 497 (1 UF, 1.500 processos triados), Profissional R$ 997 (27 UFs), Enterprise R$ 1.997. Teste 7 dias | **Declarado:** API pública DataJud/CNJ, 60 índices (27 TJ, 6 TRF, 24 TRT, STF/STJ/TST). Na curadoria, equipe percorre listas oficiais de precatórios dos tribunais "inclusive as que exigem etapas manuais" | Score explicado por processo; **régua de maturação**: dias entre trânsito em julgado e expedição por tribunal (TJSP mediana 35 dias; 9.072 processos medidos). Estágios: aguardando / aproximando / na janela / atrasado. Ente devedor, natureza do crédito, praça. Telefone "conforme disponibilidade" |
| **Guia Precatório** (guiaprecatorio.com) | Captação inbound + CRM para consultores | Assinatura mensal por consultor (valor não publicado) | **Inferência:** marketing para atrair credores (não garimpo processual); credor chega por interesse próprio | Até 50 leads/mês por consultor; triagem com IA ("Joelma") + revisão humana; entrega "caso com contexto", não lista |
| **PREC** (prec.com.br, DalaCode) | CRM/operação (lead → cessão) | Sob consulta | Não declara fonte de leads; o cliente monta "bases elegíveis" em campanhas | Priorização por valor, temperatura (quente/morno/frio), esteira jurídica com SLA, propostas e cessões, assistente IA |

## O que dá para inferir das fontes

1. **DataJud é a base comum** (JURISCAN declara; JUDIT provavelmente usa algo
   equivalente para pré-precatórios). É a mesma API pública que o Aspin Garimpo já usa.
2. **Sinal de expedição**: o movimento da Tabela Processual Unificada do CNJ
   **12457 – "Expedição de precatório/rpv"** existe no DataJud de TJSP, TRF1, TRF3
   etc. (conferido na API em 19/10/2026). É o filtro preciso que o bot passou a usar.
3. **Pré-precatório = trânsito em julgado sem expedição ainda.** A "régua" da
   JURISCAN mede o intervalo entre os dois movimentos por tribunal. Com o DataJud dá
   para reproduzir isso legitimamente.
4. **Valor e beneficiário não vêm do DataJud** (só metadados e movimentos). Vêm de:
   publicações (DJEN – que o bot já lê), listas/ordem cronológica de precatórios
   publicadas pelos tribunais e LOAs. A JUDIT fala explicitamente em LOAs 2024–2028.
5. **Contato do credor** (telefone) vem de enriquecimento por bases de terceiros.
   Isso envolve LGPD e base legal própria — não recomendado sem parecer jurídico.
6. **O diferencial comercial está na qualificação**, não só na coleta: score
   explicado, estágio do processo, ente devedor (risco de pagamento), natureza
   (alimentar x comum) e fila de trabalho por consultor.

## O que trazer para o Aspin Garimpo (proposta, em ordem de valor/esforço)

| # | Ideia | Inspiração | Como fazer de forma legítima | Status |
|---|---|---|---|---|
| 1 | Filtro por movimento 12457 no DataJud com paginação e cursor | JURISCAN | API pública DataJud | **Feito** nesta entrega |
| 2 | Cruzar DJEN (texto com valor) com DataJud (expedição confirmada) e mostrar no alerta | JURISCAN score | Já gravado em `source_documents`; alerta mostra "DataJud: expedição registrada" | **Feito** |
| 3 | Pré-filtro por valor citado e fila priorizada pelo maior valor | JUDIT filtro valor / PREC priorização | Regex local antes da IA | **Feito** |
| 4 | **Pré-precatórios**: processos com trânsito em julgado (TPU 848, código a confirmar na API) e sem 12457, em execução contra a Fazenda | JUDIT pré-precatórios, JURISCAN régua | DataJud: consulta por classe "Cumprimento de sentença contra a Fazenda Pública" + movimentos | Próximo passo |
| 5 | Régua de maturação por tribunal (mediana de dias trânsito → expedição) | JURISCAN | Calcular sobre os próprios dados coletados no DataJud | Próximo passo |
| 6 | Classificar ente devedor (União/INSS, Estado SP, município) e natureza (alimentar/comum) no prompt da IA e no alerta | JUDIT/JURISCAN filtros | Campo extra no JSON da IA | Próximo passo |
| 7 | Listas oficiais de precatórios/ordem cronológica e LOA | JUDIT LOAs | Baixar arquivos **publicados abertamente** (sem captcha) — ex.: portais de precatórios dos TRFs/TJs que oferecem CSV/PDF; LOA no Tesouro/SIOP | Avaliar fonte a fonte |
| 8 | Diário oficial municipal aberto (Querido Diário / Open Knowledge Brasil) | — | API pública api.queridodiario.org.br (estava instável: HTTP 522 em 19/10) | Avaliar |
| 9 | Fila de trabalho/pipeline (status do lead, consultor) | PREC, Guia Precatório | Tabela `opportunities.status` já existe; faltam telas | Fora do bot |
| 10 | Enriquecimento de contato do credor | JUDIT, JURISCAN | Só com base legal LGPD e fornecedor licenciado | Não recomendado agora |

## TJSP / eSAJ

As publicações do TJSP **já chegam pelo DJEN** (ex.: em 16/10/2026 o DJEN
retornou 3.541 publicações do TJSP para "ofício requisitório") e o DataJud tem o
índice `api_publica_tjsp`. A consulta direta ao eSAJ, que exige captcha, continua
fora do escopo. Se for necessário dado do eSAJ, o caminho é um provedor licenciado
(API JUDIT, Escavador).
