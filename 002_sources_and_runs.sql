create table if not exists public.source_documents (
  id uuid primary key default gen_random_uuid(),
  source text not null,
  tribunal_alias text,
  external_id text,
  process_number text,
  published_at timestamptz,
  title text,
  text_excerpt text,
  document_url text,
  raw_data jsonb not null default '{}'::jsonb,
  first_seen_at timestamptz not null default now(),
  unique(source, external_id)
);
create index if not exists source_documents_published_idx on public.source_documents(published_at desc);
create table if not exists public.collection_runs (
  id uuid primary key default gen_random_uuid(),
  mode text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  sources jsonb not null default '[]'::jsonb,
  tribunals_count integer not null default 0,
  candidates_count integer not null default 0,
  errors jsonb not null default '[]'::jsonb
);
