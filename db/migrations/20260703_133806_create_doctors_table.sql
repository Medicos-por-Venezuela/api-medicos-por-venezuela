-- Migración: create doctors table
-- Registro de médicos (reconstruido de cero). Nombres de columnas en inglés.
-- Idempotente y transaccional (el runner la envuelve en BEGIN/COMMIT).

create table if not exists public.doctors (
    id                   uuid primary key default gen_random_uuid(),
    professional_type_id uuid references public.professional_types (id) on delete set null,
    specialty_id         uuid references public.specialties (id) on delete set null,
    cedula               text not null,
    full_name            text not null,
    license              text,
    phone                text not null,
    email                text not null,
    country_of_residence text,
    -- Estado del médico: 0 = se dio de baja, 1 = activo, 2 = expulsado por admin.
    status               smallint not null default 1 check (status in (0, 1, 2)),
    -- Credenciales verificadas -> puede trabajar. Lo fija el backend al registrar
    -- (true si la cédula es válida en el SACS/FPV; false si no). Ver servicio.
    verified             boolean not null default true,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    deleted_at           timestamptz,

    -- Cédula venezolana: V-12345678 o E-12345678.
    constraint doctors_cedula_format check (cedula ~ '^[VE]-\d{6,9}$'),
    -- Teléfono internacional: +<prefijo><número>, p. ej. +5804145200715.
    constraint doctors_phone_format check (phone ~ '^\+\d{7,15}$')
);

-- Un médico activo = una cédula / un correo (ignora los borrados lógicos).
create unique index if not exists uq_doctors_cedula_not_deleted
    on public.doctors (cedula) where deleted_at is null;
create unique index if not exists uq_doctors_email_not_deleted
    on public.doctors (lower(email)) where deleted_at is null;

-- Índices para los joins/filtros habituales.
create index if not exists idx_doctors_professional_type on public.doctors (professional_type_id);
create index if not exists idx_doctors_specialty on public.doctors (specialty_id);
create index if not exists idx_doctors_deleted_at on public.doctors (deleted_at);

-- RLS: Supabase expone las tablas public vía PostgREST (anon key). Se activa
-- (sin policies = deny-all a anon/authenticated); la API accede como owner y la
-- omite. Añadir policies solo si el frontend necesita acceso directo a Supabase.
alter table public.doctors enable row level security;
