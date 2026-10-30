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

Coletor em produção via GitHub Actions (DataJud + IA + Telegram) com estados de
falha explícitos. Leia [docs/OPERACAO.md](docs/OPERACAO.md) para interpretar cada
execução, simular falhas e ver os passos pendentes (DJEN exige servidor no Brasil).
Estudo das plataformas pagas: [docs/ESTUDO_CONCORRENTES.md](docs/ESTUDO_CONCORRENTES.md).

## Configuração

Copie `.env.example` para `.env` e preencha os segredos. Nunca commite `.env`.

**DataJud e uso comercial:** o termo de uso da API pública do DataJud (CNJ) proíbe uso comercial.
`ENABLE_DATAJUD=false` (variável do repositório no GitHub) desliga toda chamada ao DataJud: coleta, sinal de expedição e `/processo`.
O padrão continua `true` até a decisão jurídica; desligado, o critério "Confirmado no DataJud" da Nota ASPIN nunca pontua.

```bash
cp .env.example .env
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
