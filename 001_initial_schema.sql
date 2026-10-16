create extension if not exists pgcrypto;

create table if not exists public.tribunals (
  id uuid primary key default gen_random_uuid(),
  alias text unique not null,
  name text not null,
  enabled boolean not null default true,
  source_type text not null default 'datajud',
  created_at timestamptz not null default now()
);

create table if not exists public.processes (
  id uuid primary key default gen_random_uuid(),
  tribunal_id uuid references public.tribunals(id),
  cnj_number text not null,
  class_name text,
  subject text,
  parties jsonb not null default '[]'::jsonb,
  last_movement_at timestamptz,
  source_url text,
  raw_data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tribunal_id, cnj_number)
);

create table if not exists public.evidence (
  id uuid primary key default gen_random_uuid(),
  process_id uuid not null references public.processes(id) on delete cascade,
  evidence_type text not null,
  event_date timestamptz,
  text_excerpt text not null,
  document_url text,
  sha256 text,
  source_payload jsonb not null default '{}'::jsonb,
  captured_at timestamptz not null default now()
);

create table if not exists public.opportunities (
  id uuid primary key default gen_random_uuid(),
  process_id uuid not null references public.processes(id) on delete cascade,
  evidence_id uuid references public.evidence(id),
  status text not null default 'new',
  requisition_type text,
  amount numeric(18,2),
  beneficiary text,
  debtor_entity text,
  lawyer text,
  issued_at timestamptz,
  deadline_at timestamptz,
  potential_score numeric(5,4),
  classification jsonb not null default '{}'::jsonb,
  alerted_at timestamptz,
  created_at timestamptz not null default now(),
  unique (process_id, evidence_id)
);

create table if not exists public.audit_log (
  id uuid primary key default gen_random_uuid(),
  action text not null,
  entity_type text,
  entity_id uuid,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

insert into public.tribunals (alias, name) values
  ('api_publica_trf1', 'Tribunal Regional Federal da 1ª Região')
on conflict (alias) do nothing;
