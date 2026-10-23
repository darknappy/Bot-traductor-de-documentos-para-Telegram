-- Esquema de Supabase para el bot traductor.
--
-- Cómo usarlo:
--   1. Entra en tu proyecto de Supabase → SQL Editor → New query.
--   2. Pega todo este archivo y pulsa Run.
--   3. Crea (o deja que el bot cree) el bucket privado «traductor» en Storage.
--
-- Usa la clave service_role en Render. No pongas esta clave en el repositorio.
-- Las tablas tienen RLS activado y ninguna política: solo la service_role
-- (que se salta RLS) puede leerlas y escribirlas.

create table if not exists public.bot_jobs (
    id                 text primary key,
    user_id            bigint not null,
    chat_id            bigint not null,
    status_message_id  bigint,
    src_message_id     bigint,
    file_id            text,
    file_name          text not null,
    size_bytes         bigint not null default 0,
    lang               text not null default 'es',
    engine             text not null default 'auto',
    mode               text not null default 'translate',
    split_mode         text,
    split_value        integer,
    status             text not null default 'queued',
    input_key          text,
    output_key         text,
    output_name        text,
    progress           real not null default 0,
    progress_note      text,
    error              text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create index if not exists bot_jobs_status_idx
    on public.bot_jobs (status, created_at);

create index if not exists bot_jobs_user_idx
    on public.bot_jobs (user_id, created_at);

create table if not exists public.bot_users (
    user_id    bigint primary key,
    lang       text not null default 'es',
    engine     text not null default 'auto',
    updated_at timestamptz not null default now()
);

alter table public.bot_jobs enable row level security;
alter table public.bot_users enable row level security;

-- Doble cierre: aunque alguien desactivara RLS por error, los clientes con la
-- clave anon/authenticated no tienen permisos sobre estas tablas.
revoke all on table public.bot_jobs from anon, authenticated;
revoke all on table public.bot_users from anon, authenticated;

-- El bucket es privado. El bot lo crea automáticamente si no existe, pero
-- también puedes crearlo a mano en Storage con el nombre «traductor».
-- (No hace falta insertar nada en storage.buckets desde SQL.)
