# Passo a passo para deixar o Garimpo ASPIN 100%

Ordem de prioridade. Os passos 1 e 2 são os que faltam para o funcionamento
completo; os demais aumentam volume e confiabilidade.

## 1. Ligar a planilha (10 min) — obrigatório para as aprovações irem ao Sheets

1. Abra a planilha **ASPIN Garimpo — Oportunidades Aprovadas**:
   https://docs.google.com/spreadsheets/d/15EG95-C-36Z3Ejb0Ag7IIubAiH-_GvXzPfDg4fWEy-8/edit
2. Menu **Extensões → Apps Script**.
3. Apague todo o código que aparecer e cole o conteúdo do arquivo
   `D:\aspin-garimpo-agent-bot\PLANILHA_COLAR_NO_APPS_SCRIPT.gs`
   (abra no Bloco de Notas, Ctrl+A, Ctrl+C). Clique no disquete para salvar.
4. **Implantar → Nova implantação** → na engrenagem "Selecionar tipo" escolha
   **App da Web**:
   - Executar como: **Eu**
   - Quem pode acessar: **Qualquer pessoa**
   - Clique **Implantar** → **Autorizar acesso** → escolha sua conta →
     **Avançado** → **Acessar projeto** → **Permitir**.
5. Copie a **URL do app da Web** (termina em `/exec`).
6. No GitHub: repositório → **Settings → Secrets and variables → Actions →
   New repository secret**:
   - Nome `SHEETS_WEBHOOK_URL`, valor = a URL do passo 5.
   - Nome `SHEETS_TOKEN`, valor = o conteúdo de
     `D:\aspin-garimpo-agent-bot\PLANILHA_TOKEN.txt`.
7. **Actions → Botões Telegram → Run workflow**. As aprovações já feitas
   aparecem na planilha em ~1 min. (Teste: abrir a URL do passo 5 no navegador
   deve mostrar `{"ok":true,"service":"aspin-planilha"}`.)

## 2. Rodar o workflow na sua VM (30 min) — minutos ilimitados, DJEN e horário confiável

Por quê: nos servidores do GitHub (1) a cota grátis de repositório privado é
2.000 min/mês e a coleta de hora em hora gasta bem mais; (2) a API do DJEN
recusa IPs fora do Brasil; (3) o agendamento do GitHub atrasa ou pula horários.

1. Confirme que a VM está no Brasil (Oracle → Compute → Instances → região
   `sa-saopaulo-1` ou `sa-vinhedo-1`).
2. Entre na VM por SSH e pare o worker antigo (ele roda a versão velha e manda
   alertas no formato antigo):
   ```bash
   cd /home/ubuntu/aspin-garimpo && sudo docker compose down
   ```
3. Teste o DJEN a partir da VM (tem que responder `200`):
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" "https://comunicaapi.pje.jus.br/api/v1/comunicacao?texto=precat%C3%B3rio&itensPorPagina=1&pagina=1&meio=D"
   ```
4. No GitHub: **Settings → Actions → Runners → New self-hosted runner → Linux x64**.
   Rode na VM os comandos que a página mostra (Download e Configure), aceitando
   os padrões. Depois instale como serviço:
   ```bash
   sudo ./bin/installdependencies.sh
   sudo ./svc.sh install
   sudo ./svc.sh start
   ```
5. A página de Runners deve mostrar o runner **Idle** (verde).
6. Em **Settings → Secrets and variables → Actions → Variables → New repository variable**:
   - `RUNNER` = `self-hosted`
   - `ENABLE_DJEN` = `true`

## 3. Limite da IA (5 min) — para chegar às 25 classificações por hora

Os planos gratuitos do Groq e do Gemini estão no limite (erro 429). O mais
simples: em https://console.groq.com → **Settings → Billing**, ative o plano pago
(Developer). A chave continua a mesma; nada muda no GitHub.

## 4. Corrigir a DATABASE_URL no secret `ACCS` (5 min)

O bot corrige em memória, mas o valor salvo tem espaço no meio e a região
`ca-central1` (o certo é `ca-central-1`). No Supabase: **Connect → Session
pooler → copiar URI** (uma linha). No GitHub, edite o secret `ACCS` e troque só
a linha `DATABASE_URL=...`.

## 5. (Opcional) Restringir quem aprova

Por padrão qualquer membro do canal pode clicar. Para restringir, crie a
variável `TELEGRAM_APPROVERS` com os IDs numéricos do Telegram (ex.: obtidos com
o @userinfobot), separados por vírgula.

## Como conferir que está tudo certo

- **Actions → Garimpo ASPIN**: runs verdes de hora em hora; o resumo de cada run
  mostra páginas, sinais, classificações e erros.
- **Actions → Botões Telegram**: a cada 10 min; o resumo mostra cliques,
  aprovadas, descartadas e linhas gravadas na planilha.
- **Telegram**: às 08h, 12h e 18h chegam as 3 melhores; ao clicar, o botão vira
  "✅ Aprovado por Fulano" em até ~10 min.
