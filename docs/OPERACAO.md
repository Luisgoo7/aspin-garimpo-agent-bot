# Operação do Garimpo ASPIN (GitHub Actions)

## Como ler o resultado de cada execução

Cada execução grava uma linha em `public.collection_runs` (com `finished_at`,
`outcome`, `metrics` e `errors`) e escreve um resumo sanitizado na aba **Summary**
do run no GitHub.

| outcome | Significado | Exit code | Workflow |
|---|---|---|---|
| `success` | Todas as fontes ativas responderam. `opportunities=0` aqui é um resultado real: a busca foi feita e nada novo passou nos filtros. | 0 | verde |
| `partial` | Busca feita, mas houve falha isolada (ex.: 1 tribunal fora), backlog de páginas ou fila de IA para o próximo ciclo. Itens com falha ficam em `retry`. | 0 | verde, com detalhes no Summary |
| `source_failed` | **A busca falhou**: o DJEN não devolveu nenhuma página válida, ou todos os tribunais DataJud falharam. Não é "zero oportunidades". | 3 | vermelho |
| `ai_failed` | Havia documentos e nenhuma classificação de IA deu certo (todos os provedores com erro/limite). | 4 | vermelho |
| `alert_failed` | Havia alertas e nenhum foi entregue ao Telegram. Os alertas continuam pendentes. | 4 | vermelho |
| banco inacessível | A conexão PostgreSQL falhou. O log mostra a causa sanitizada (`causa=...` e `como corrigir`). | 5 | vermelho |
| configuração ausente | Falta algum secret obrigatório. | 2 | vermelho |

### Falha passageira não deixa o workflow vermelho

Se uma fonte, a IA ou o Telegram falhar **uma vez**, o ciclo termina como
`partial` (verde), registra `falha_transitoria` nas métricas e tenta de novo na
hora seguinte. Só fica vermelho quando a **mesma** falha se repete em 3 ciclos
seguidos (`FAILURE_STREAK_TO_ALERT`) — aí é um problema que precisa de ação — ou
numa simulação deliberada.

## Entrega no Telegram: só as 3 melhores, às 08h, 12h e 18h

- A coleta roda de hora em hora e classifica até **25** documentos por ciclo.
- Às **08h, 12h e 18h (Brasília)** o bot envia **apenas as 3 melhores**
  oportunidades acumuladas desde a entrega anterior.
- "Melhor" = confiança ≥ `MIN_CONFIDENCE` (78%) e valor ≥ `MIN_OPPORTUNITY_AMOUNT`
  (R$ 100 mil), ordenadas por **valor × confiança**.
- As demais candidatas da janela ficam no banco com `status='nao_selecionada'`
  (consultáveis, mas não enviadas depois).
- Se não houver nenhuma, o canal recebe uma única mensagem curta dizendo isso.
- O GitHub às vezes atrasa o agendamento: a entrega acontece no primeiro ciclo
  até 3h depois do horário. Se o Telegram cair no meio, o ciclo seguinte envia só
  o que faltou (nunca duplica).
- Resumos de status só vão ao canal quando o coletor falha de verdade.

## Botões Aprovar/Descartar e planilha

- Cada oportunidade chega enxuta no Telegram (valor, tipo, tribunal, processo,
  credor → devedor, confiança) com os botões **✅ Aprovar** e **❌ Descartar**.
- O workflow **Botões Telegram** roda a cada 5 min: grava a decisão no banco,
  troca os botões por "✅ Aprovado por Fulano" e envia as aprovadas para a planilha
  [ASPIN Garimpo — Oportunidades Aprovadas](https://docs.google.com/spreadsheets/d/15EG95-C-36Z3Ejb0Ag7IIubAiH-_GvXzPfDg4fWEy-8/edit)
  com todos os detalhes (requisição, natureza, preferência, honorários, trânsito em julgado, fonte...).
- Vale a primeira decisão; cliques repetidos não duplicam nada.
- Se a planilha estiver fora do ar, a aprovação fica guardada no banco e é reenviada
  a cada 5 min. O run só fica vermelho após 3 falhas seguidas da planilha.
- Para restringir quem pode decidir, crie a variável `TELEGRAM_APPROVERS` com os IDs
  numéricos do Telegram separados por vírgula.

## Testar o caminho de erro (falha simulada)

Actions → **Garimpo ASPIN** → **Run workflow** → em `simulate_failure` escolha
`djen`, `datajud`, `ia`, `telegram` ou `db`. O run deve ficar **vermelho** e a
falha aparece em `collection_runs` com `mode='simulation'` (exceto `db`, que por
definição não consegue gravar no banco).

## Fontes

- **DataJud (CNJ)** — API pública `api_publica_*`. O coletor busca processos com o
  movimento TPU **12457 "Expedição de precatório/rpv"**, pagina com `search_after`
  e guarda um cursor por tribunal (`bot_state`, chave `datajud_cursor:<tribunal>`).
  O DataJud indexa com atraso de vários dias, por isso o cursor segue a data de
  indexação. Tribunais padrão: TJSP, TRF1–6, TJRJ, TJMG (variável `DATAJUD_TRIBUNALS`).
- **DJEN / Comunica PJe** — publicações com texto e valor (inclui TJSP). **A API
  recusa IPs fora do Brasil (HTTP 403)**, então não roda nos servidores do GitHub.
  Ela fica desligada por padrão no Actions (`ENABLE_DJEN=false`) e deve rodar em um
  servidor no Brasil (veja abaixo). Filtro opcional por tribunal: `DJEN_TRIBUNALS=TJSP,TRF3`.
- **TJSP/eSAJ** — só monitoramento de disponibilidade. Sem contorno de captcha.

## Pré-filtro e fila

Antes da IA, o texto passa por um pré-filtro local:

- sem termo de requisição → `ignored`
- termo, mas sem valor citado → `needs_enrichment` (sem gastar IA; o alerta exige valor)
- todos os valores abaixo de `MIN_OPPORTUNITY_AMOUNT` → `below_minimum`
- caso contrário → `pending` (vai para a IA, do maior valor para o menor)
- DataJud → `signal` (confirma a expedição no alerta do DJEN do mesmo processo)

Falha da IA → `retry` com backoff; depois de `MAX_PROCESSING_ATTEMPTS` → `failed`.
Documento só vira `classified`/`rejected` depois da resposta da IA gravada.

## Passos que dependem do responsável pelo projeto

### 1. Corrigir a DATABASE_URL dentro do secret `ACCS`

O diagnóstico do workflow mostrou dois problemas no valor atual (sem expor o valor):

- a URI tem **espaço ou quebra de linha** no meio;
- a região do pooler está escrita **`ca-central1`**, mas o host real é **`ca-central-1`**.

O código corrige os dois em memória e avisa no log, mas o secret deve ser corrigido:

1. No Supabase, abra o projeto do Garimpo → botão **Connect** → aba
   **Connection string** → **Session pooler** → copie a URI (uma linha só).
2. Troque `[YOUR-PASSWORD]` pela senha do banco (sem colchetes). Se a senha tiver
   `@`, `#`, `/` ou `:`, gere uma nova senha só com letras e números em
   **Database Settings → Reset database password**.
3. No GitHub: **Settings → Secrets and variables → Actions → `ACCS` → Update**.
   O `ACCS` é um arquivo no formato `.env`; altere **apenas** a linha
   `DATABASE_URL=...`, cole a URI numa linha só e salve.
4. Rode **Actions → Garimpo ASPIN → Run workflow** e confira que a etapa
   "Testar conexão com o banco" mostra `conexão=ok` **sem** o aviso de região.

### 2. Rodar o DJEN em um servidor no Brasil

Escolha uma opção:

- **Runner próprio do GitHub na VM (recomendado)**: na VM Ubuntu (ou em um PC
  ligado no Brasil), siga **Settings → Actions → Runners → New self-hosted runner**
  (Linux x64) e instale como serviço (`sudo ./svc.sh install && sudo ./svc.sh start`).
  Depois crie as variáveis em **Settings → Secrets and variables → Actions → Variables**:
  `RUNNER=self-hosted` e `ENABLE_DJEN=true`.
- **Worker Docker da VM**: atualize o código da VM para esta versão (backup antes,
  ver `DEPLOY.md`) e mantenha `ENABLE_DJEN=true` no `.env` da VM. Nesse caso deixe
  o Actions com DJEN desligado; o lease no banco impede dois ciclos ao mesmo tempo.

Confirme antes em qual região a VM está (Oracle → Compute → Instances). Se não
for no Brasil, o DJEN também será recusado nela.

### 3. Limite da IA

Nos testes de 19/10/2026 o Groq (plano gratuito) devolveu 429 com espera de 60s.
O coletor agora troca para Gemini/NVIDIA na hora e pausa o provedor limitado por
10 minutos. Se os 429 forem frequentes, considerar plano pago em pelo menos um
provedor ou reduzir `MAX_CLASSIFY_PER_CYCLE`.
