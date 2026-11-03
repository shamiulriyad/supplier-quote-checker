-- Supplier Quote Checker: application schema (PostgreSQL / Supabase).
-- Idempotent: safe to run more than once. The app also applies it on start.
-- LangGraph's own checkpoint tables (checkpoints, checkpoint_blobs, checkpoint_writes,
-- checkpoint_migrations) are created by PostgresSaver.setup(), see tools/supabase_setup.py.

create extension if not exists pgcrypto;

create table if not exists batches (
    batch_id     text primary key,
    mode         text not null,
    status       text not null default 'queued'
                 check (status in ('queued', 'running', 'finished', 'cancelled', 'interrupted')),
    total        integer not null default 0,
    started_at   timestamptz,
    finished_at  timestamptz,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create table if not exists documents (
    id                 uuid primary key default gen_random_uuid(),
    batch_id           text not null references batches(batch_id) on delete cascade,
    position           integer not null,
    file_hash          text check (file_hash ~ '^[0-9a-f]{64}$'),
    original_filename  text not null,
    storage_path       text,
    size_bytes         bigint,
    supplier           text,
    item               text,
    decision           text check (decision in ('APPROVE', 'REJECT', 'NEEDS_REVIEW')),
    extraction_source  text,
    extracted_text     text,
    result_json        jsonb,
    processing_status  text not null default 'queued'
                       check (processing_status in ('queued', 'running', 'completed', 'waiting_review',
                                                    'reviewing', 'reviewed', 'failed', 'cancelled',
                                                    'interrupted', 'deleting')),
    failed_stage       text,
    error              text,
    seconds            double precision,
    thread_id          text,
    human_decision     text check (human_decision in ('APPROVE', 'REJECT')),
    human_comment      text,
    reviewed_at        timestamptz,
    processed_at       timestamptz,
    legacy_id          text unique,          -- id from the old SQLite history, when migrated
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    unique (batch_id, file_hash),
    unique (batch_id, position)
);
create index if not exists documents_batch_idx on documents (batch_id);
create index if not exists documents_status_idx on documents (processing_status);
create index if not exists documents_hash_idx on documents (file_hash);
create index if not exists documents_processed_idx on documents (processed_at desc);

create table if not exists document_events (
    id           bigint generated always as identity primary key,
    batch_id     text not null references batches(batch_id) on delete cascade,
    document_id  uuid references documents(id) on delete cascade,   -- null for batch-level events
    event_type   text not null,                                      -- '<stage>.<status>', e.g. 'parse.completed'
    event_data   jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now()
);
create index if not exists document_events_doc_idx on document_events (document_id, id);
create index if not exists document_events_batch_idx on document_events (batch_id, id);

create table if not exists pipeline_checkpoints (
    id               bigint generated always as identity primary key,
    document_id      uuid not null references documents(id) on delete cascade,
    step_name        text not null,          -- a LangGraph node: parse, extract, ..., human_review, report
    status           text not null check (status in ('pending', 'running', 'completed', 'failed',
                                                     'waiting', 'skipped')),
    checkpoint_data  jsonb not null default '{}'::jsonb,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    unique (document_id, step_name)
);

create table if not exists llm_cache (
    cache_key       text primary key,        -- sha256(prompt version | document text)[:16]
    model           text,
    prompt_version  text,
    document        text,                    -- file name it was first created for (informational)
    response        jsonb not null,
    created_at      timestamptz not null default now()
);

-- keep updated_at current
create or replace function set_updated_at() returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end $$;

do $$
declare t text;
begin
    foreach t in array array['batches', 'documents', 'pipeline_checkpoints'] loop
        if not exists (select 1 from pg_trigger where tgname = t || '_updated_at') then
            execute format('create trigger %I before update on %I for each row execute function set_updated_at()',
                           t || '_updated_at', t);
        end if;
    end loop;
end $$;

-- Row Level Security: on, with no policies. The anon/authenticated API roles can read nothing;
-- the app connects server-side (database URL / service role), which bypasses RLS.
-- Add per-user policies here when authentication is introduced.
alter table batches enable row level security;
alter table documents enable row level security;
alter table document_events enable row level security;
alter table pipeline_checkpoints enable row level security;
alter table llm_cache enable row level security;
