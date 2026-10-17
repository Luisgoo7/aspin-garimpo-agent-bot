# Implantação ASPIN Garimpo v0.3

O código principal fica em `app/`. O bot exige PostgreSQL, Telegram e pelo menos um provedor de IA. DataJud é obrigatório quando `ENABLE_DATAJUD=true`. Não commite nem imprima credenciais.

## Atualizar a VM existente

Preserve o `.env`, faça backup do diretório e do banco e instale o código atualizado.

```bash
python -m app.worker --check-config
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 worker
```

A primeira execução aplica as migrações SQL 001, 002 e 003. São aditivas e idempotentes; não removem documentos nem oportunidades. O usuário do banco precisa poder criar tabelas e adicionar colunas. Se não puder, aplique as migrações como administrador antes de subir o worker.

## Agendamento e credenciais

O worker contínuo e o Actions usam trava PostgreSQL para impedir duas coletas simultâneas. Escolha um responsável primário pelo agendamento para evitar trabalho redundante.

O workflow executa testes, verifica configuração e roda um ciclo. Cadastre separadamente no GitHub Actions: `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATAJUD_API_KEY` e pelo menos um de `GROQ_API_KEY`, `GEMINI_API_KEY`, `NVIDIA_API_KEY`. A autorização de leitura de logs não necessariamente permite administrar secrets.

DJEN pode negar origens geográficas (HTTP 403). Nesse caso o bot registra erro e não anuncia ausência de oportunidades. Use um ambiente autorizado com acesso à fonte; não contorne bloqueios nem CAPTCHA. O cron do GitHub também pode atrasar.

## Validação de produção

Código publicado e teste local não confirmam a produção. É necessário verificar resposta real das fontes, classificação com o provedor de produção, gravação no banco, entrega no Telegram e ao menos um ciclo automático.

```sql
select started_at,finished_at,outcome,candidates_count,metrics,errors
from public.collection_runs order by started_at desc limit 5;
select processing_status,count(*) from public.source_documents group by processing_status;
select created_at,amount,potential_score,alerted_at from public.opportunities order by created_at desc limit 10;
```

`success` significa operações concluídas sem erros nessa janela; `partial` significa limites de cobertura; `degraded` ou `failed` significa falha. Nenhum deles afirma inexistência de créditos no mercado. O status informa contagens, fila e erros, respeitando `STATUS_INTERVAL_MINUTES`. `/health` responde 503 quando faltam configuração, registro recente ou execução saudável.

## Fila e alertas

Documentos ficam em fila antes da IA e só são concluídos após classificação/persistência. Falhas de IA, rede ou gravação geram retry com atraso e limite de tentativas. Depois do limite ficam em `failed` para revisão. Documento alterado volta à fila; os registros antigos sem classificação entram como pendentes. Crédito plausível sem valor explícito fica em `needs_enrichment`, sem valor inventado.

Alertas não confirmados continuam pendentes. A deduplicação normaliza tribunal e CNJ. Se Telegram aceitar uma mensagem e sua resposta HTTP se perder, um reenvio ainda pode duplicá-la: `sendMessage` não oferece chave de idempotência. Não se promete entrega exatamente uma vez.

## Testes

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q
ASPIN_TEST_DATABASE_URL=postgresql:///aspin_test python -m pytest tests -q
```

A integração SQL só pode usar banco descartável chamado `aspin_test`; esses testes truncam suas tabelas. Nunca use o banco de produção.

## Limites reais

DJEN tem disponibilidade e rate limit externos. A paginação retoma em ciclos seguintes quando atinge limites. TJSP/eSAJ ainda é monitoramento, não extração autenticada de ofícios. DataJud é fonte de metadados; alguns créditos exigem documento adicional. Todo candidato enviado precisa de revisão jurídica/comercial de situação atual, titularidade, disponibilidade e valor negociável. Comissão indicada é estimativa, não lucro nem retorno garantido.
