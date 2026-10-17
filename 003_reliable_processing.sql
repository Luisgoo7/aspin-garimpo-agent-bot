-- Migração aditiva e idempotente. Não remove dados existentes.
alter table public.source_documents add column if not exists content_hash text;
alter table public.source_documents add column if not exists processing_status text not null default 'pending';
alter table public.source_documents add column if not exists processing_attempts integer not null default 0;
alter table public.source_documents add column if not exists processing_started_at timestamptz;
alter table public.source_documents add column if not exists next_attempt_at timestamptz not null default now();
alter table public.source_documents add column if not exists processed_at timestamptz;
alter table public.source_documents add column if not exists last_error text;
alter table public.source_documents add column if not exists classification jsonb;
alter table public.source_documents add column if not exists updated_at timestamptz not null default now();
alter table public.collection_runs add column if not exists outcome text not null default 'running';
alter table public.collection_runs add column if not exists metrics jsonb not null default '{}'::jsonb;
create index if not exists source_documents_pending_idx on public.source_documents(processing_status,next_attempt_at);
create table if not exists public.bot_state (
    key text primary key,
    value jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);
