-- Migração aditiva e idempotente: retenção de 48 h.
-- Rastro mínimo das oportunidades já apagadas, para o bot não notificar de novo a mesma
-- (chave = tribunal|processo|tipo; sem nome, valor ou texto).
create table if not exists public.opportunity_tombstones (
  key text primary key,
  created_at timestamptz not null default now()
);
create index if not exists opportunities_created_idx on public.opportunities(created_at);
create index if not exists source_documents_first_seen_idx on public.source_documents(first_seen_at);
