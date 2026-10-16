# Implantação ASPIN Garimpo Judicial

## Já concluído

- Projeto Supabase `aspin-garimpo-judicial` criado.
- Migração inicial aplicada: tribunais, processos, evidências, oportunidades e auditoria.
- API DataJud, filtros de palavras-chave, classificador Groq/Gemini e alerta Telegram implementados.

## Pendências de ativação

1. Criar uma VM Always Free no Oracle Cloud em São Paulo.
2. Instalar Docker, copiar o projeto e preencher `.env`.
3. Criar um bot no BotFather e informar o `TELEGRAM_BOT_TOKEN` apenas no servidor.
4. Obter `TELEGRAM_CHAT_ID` enviando `/start` ao bot.
5. Configurar `DATAJUD_API_KEY`, `GROQ_API_KEY` e/ou `GEMINI_API_KEY` no servidor.
6. Subir `docker compose up -d` e validar `/health`.

As chaves não devem ser commitadas nem enviadas no chat. O classificador usa regras antes da IA e mantém fallback entre Groq e Gemini; isso é prompt engineering/RAG leve e avaliação, não treinamento proprietário dos modelos.

## Atualização v0.2 (correções do garimpo)
- DJEN agora alimenta a classificação (antes só salvava).
- Falhas da IA aparecem no log e em `collection_runs.errors`.
- Sem alerta repetido: um processo só gera uma oportunidade por tipo.
- Filtro por palavra-chave corrigido (antes deixava tudo passar).
- Novas variáveis opcionais: `DATAJUD_PAGE_SIZE` (20) e `MAX_CLASSIFY_PER_CYCLE` (60).
