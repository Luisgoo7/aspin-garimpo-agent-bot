# ASPIN Garimpo Judicial

MVP para localizar eventos processuais relacionados a ofícios requisitórios, RPVs e precatórios, classificar evidências com IA e enviar alertas pelo Telegram.

## Arquitetura inicial

- **FastAPI**: API interna e healthcheck.
- **Worker assíncrono**: coleta DataJud, normaliza eventos e grava evidências.
- **Supabase/Postgres**: processos, eventos, evidências, oportunidades e auditoria.
- **Telegram Bot**: alertas e comandos operacionais.
- **Groq/Gemini**: classificação estruturada de evidências selecionadas. Não é treinamento do modelo; é classificação com prompt versionado, exemplos e avaliação.
- **Oracle Cloud**: hospedagem 24/7 do worker e da API via Docker Compose.

## Status

Esqueleto de produção criado. Para ativar são necessários: projeto Supabase, credenciais da Oracle, token do BotFather e chaves Groq/Gemini.

## Configuração

Copie `.env.example` para `.env` e preencha os segredos. Nunca commite `.env`.

```bash
cp .env.example .env
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
