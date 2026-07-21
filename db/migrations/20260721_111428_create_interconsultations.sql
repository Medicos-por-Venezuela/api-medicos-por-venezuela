-- Migración: tabla `interconsultations` — segunda opinión EN TIEMPO REAL durante una consulta
-- activa (la consulta sigue abierta). Ver .knowledge/interconsultas.md. No confundir con
-- "Agendar con Especialista" (que cierra la consulta y agenda para otro día).
-- Solo la API accede (RLS deny-all: enable RLS sin policies, la API es owner y la bypasea).
-- Idempotente.

create table if not exists public.interconsultations (
    id                uuid primary key default gen_random_uuid(),
    -- Consulta inter-consultada. 1 interconsulta por consulta (por ahora) -> índice único abajo.
    consultation_id   uuid not null references public.consultations(id) on delete cascade,
    -- user_id (profiles.id) del médico INVITADO (ve datos limitados: motivo, notas, edad).
    invited_doctor_id uuid not null,
    -- user_id del médico que ATIENDE la consulta (quien crea la interconsulta).
    created_by_id     uuid not null,
    status            text not null default 'active',  -- por ahora solo 'active'
    note              text,                             -- mensaje/razón opcional del que invita
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

-- 1 interconsulta por consulta (por ahora: 1 médico para 1 interconsulta).
create unique index if not exists uq_interconsultations_consultation
    on public.interconsultations (consultation_id);

-- Listar rápido las interconsultas asignadas a un médico invitado.
create index if not exists ix_interconsultations_invited_doctor
    on public.interconsultations (invited_doctor_id);

alter table public.interconsultations enable row level security;
