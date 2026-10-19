-- Migração aditiva e idempotente: decisão humana (Aprovar/Descartar) e envio à planilha.
alter table public.opportunities add column if not exists telegram_chat_id text;
alter table public.opportunities add column if not exists telegram_message_id bigint;
alter table public.opportunities add column if not exists decision text;
alter table public.opportunities add column if not exists decided_by text;
alter table public.opportunities add column if not exists decided_at timestamptz;
alter table public.opportunities add column if not exists sheet_synced_at timestamptz;
alter table public.opportunities add column if not exists sheet_error text;
create index if not exists opportunities_sheet_outbox_idx on public.opportunities(decision, sheet_synced_at);
