-- Migración: create interconsultation requests
-- Creada:    2026-08-31 17:24:26
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- Tabla `interconsultation_requests` — segunda opinión ASÍNCRONA sobre un paciente de
-- consultorio (ver tasks/interconsulta-asincrona/spec.md).
--
-- ¿Por qué una tabla NUEVA y no reusar `interconsultations`? Porque aquella exige
-- `consultation_id` NOT NULL + UNIQUE: nace de una consulta ACTIVA de la cola, con video
-- compartido y los dos médicos conectados a la vez. Este flujo NO tiene consulta: el paciente
-- ni siquiera es usuario de la plataforma. Meterlos en la misma tabla obligaría a aflojar esa
-- FK y a que cada query distinguiera los dos casos. Son cuatro flujos distintos y hay que
-- mantenerlos distintos (ver .knowledge/interconsultas.md).
--
-- Solo la API accede (RLS deny-all: enable RLS sin policies; la API es owner y la bypasea).

create table if not exists public.interconsultation_requests (
    id                   uuid primary key default gen_random_uuid(),

    -- El caso. Un mismo paciente puede tener varias solicitudes a lo largo del tiempo (por eso
    -- no hay índice único acá). `cascade`: si se purga el paciente, muere su solicitud.
    patient_id           uuid not null references public.patients(id) on delete cascade,

    -- users.id del MÉDICO TRATANTE (quien pide la ayuda). Es también el único que puede
    -- cancelar y cerrar. `restrict`: no se borra una cuenta dejando solicitudes sin dueño.
    requesting_doctor_id uuid not null references public.users(id) on delete restrict,

    -- 'specialty' = difusión a todos los médicos de la especialidad (el modo principal).
    -- 'doctor'    = dirigida a UN médico concreto (modo secundario).
    mode                 text not null,

    -- Especialidad buscada. En modo 'doctor' se deriva del destinatario y se guarda igual,
    -- para que la bandeja y las métricas no tengan que ramificar por modo.
    specialty_id         uuid not null references public.specialties(id) on delete restrict,

    -- Destinatario único en modo 'doctor'. NULL en modo 'specialty' (ver ck_..._target).
    target_doctor_id     uuid references public.users(id) on delete set null,

    -- Contexto clínico que ve el especialista ANTES de tomar. NUNCA lleva identidad del
    -- paciente: eso lo garantiza el schema Pydantic de salida, no esta tabla.
    chief_complaint      text not null,
    clinical_notes       text,

    -- open -> taken -> closed  |  open -> cancelled. Ver ck_..._status y la máquina de
    -- estados en la spec. `closed` lo fija el TRATANTE, nunca el especialista.
    status               text not null default 'open',

    -- Especialista que ganó la carrera. Uno solo por caso (decisión de producto, no técnica).
    taken_by_doctor_id   uuid references public.users(id) on delete set null,
    taken_at             timestamptz,

    closed_at            timestamptz,
    -- Opcional al cerrar. Todavía no se muestra en ningún lado: se guarda desde ya para que el
    -- historial de la próxima iteración no nazca sin nada que mostrar.
    closing_note         text,

    cancelled_at         timestamptz,

    -- Cuántos correos salieron en el fan-out. Diagnóstico: si un médico dice "no me llegó",
    -- este número dice si el problema fue el filtro de destinatarios o el envío.
    notified_count       integer not null default 0,

    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

-- === Invariantes en la BD, no solo en Pydantic ===
-- Pydantic protege la puerta HTTP; estos CHECK protegen la tabla de cualquier otra vía
-- (scripts, migraciones futuras, psql a mano).
do $$
begin
    -- Los cuatro estados de la máquina. Nada más.
    if not exists (select 1 from pg_constraint
                   where conname = 'ck_interconsultation_requests_status'
                     and conrelid = 'public.interconsultation_requests'::regclass) then
        alter table public.interconsultation_requests
            add constraint ck_interconsultation_requests_status
            check (status in ('open', 'taken', 'closed', 'cancelled'));
    end if;

    -- Los dos modos de solicitud.
    if not exists (select 1 from pg_constraint
                   where conname = 'ck_interconsultation_requests_mode'
                     and conrelid = 'public.interconsultation_requests'::regclass) then
        alter table public.interconsultation_requests
            add constraint ck_interconsultation_requests_mode
            check (mode in ('specialty', 'doctor'));
    end if;

    -- target_doctor_id existe SII el modo es 'doctor'. Sin esto podría quedar una solicitud
    -- "dirigida" a nadie, o una difusión con un destinatario fantasma que nadie lee.
    if not exists (select 1 from pg_constraint
                   where conname = 'ck_interconsultation_requests_target'
                     and conrelid = 'public.interconsultation_requests'::regclass) then
        alter table public.interconsultation_requests
            add constraint ck_interconsultation_requests_target
            check (
                (mode = 'doctor'    and target_doctor_id is not null)
                or (mode = 'specialty' and target_doctor_id is null)
            );
    end if;

    -- Un caso tomado tiene quién y cuándo; uno no tomado no tiene ninguno de los dos.
    -- Evita el estado imposible "taken sin especialista" que rompería la vista del tratante.
    if not exists (select 1 from pg_constraint
                   where conname = 'ck_interconsultation_requests_taken'
                     and conrelid = 'public.interconsultation_requests'::regclass) then
        alter table public.interconsultation_requests
            add constraint ck_interconsultation_requests_taken
            check (
                (taken_by_doctor_id is null     and taken_at is null)
                or (taken_by_doctor_id is not null and taken_at is not null)
            );
    end if;
end $$;

-- === Índices ===

-- La bandeja del especialista: solicitudes ABIERTAS de su especialidad. Es la query más
-- caliente del feature (la corre cada especialista cada vez que entra al panel).
create index if not exists ix_interconsultation_requests_inbox
    on public.interconsultation_requests (specialty_id, created_at desc)
    where status = 'open';

-- "Mis solicitudes" del médico tratante.
create index if not exists ix_interconsultation_requests_requester
    on public.interconsultation_requests (requesting_doctor_id, created_at desc);

-- "Casos activos que tomé" del especialista.
create index if not exists ix_interconsultation_requests_taken_by
    on public.interconsultation_requests (taken_by_doctor_id)
    where taken_by_doctor_id is not null;

-- Las dirigidas a un médico concreto (parcial: en modo 'specialty' la columna es NULL).
create index if not exists ix_interconsultation_requests_target
    on public.interconsultation_requests (target_doctor_id)
    where target_doctor_id is not null;

-- Solo la API lee y escribe esta tabla.
alter table public.interconsultation_requests enable row level security;

comment on table public.interconsultation_requests is
    'Solicitudes de interconsulta ASÍNCRONA sobre pacientes de consultorio. Distinta de '
    '`interconsultations` (segunda opinión EN VIVO durante una consulta activa de la cola).';
