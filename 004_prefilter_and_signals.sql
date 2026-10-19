-- Migração aditiva e idempotente. Não remove dados existentes.
-- Maior valor citado no texto (pré-filtro local) para priorizar a fila da IA.
alter table public.source_documents add column if not exists prefilter_amount numeric(18,2);
create index if not exists source_documents_process_idx on public.source_documents(source, process_number);
create index if not exists source_documents_claim_idx on public.source_documents(processing_status, prefilter_amount desc nulls last);
