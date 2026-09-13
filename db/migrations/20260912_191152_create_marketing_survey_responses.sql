-- Migración: create marketing survey responses
-- Creada:    2026-09-12 19:11:52
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- Tabla `marketing_survey_responses` — respuestas a las encuestas de re-targeting que se mandan
-- por correo masivo a tres segmentos de médicos: psicólogos, especialistas y médicos generales.
--
-- Una fila por (encuesta, correo): volver a responder ACTUALIZA la fila en vez de sumar otra. Lo
-- promete la propia encuesta ("si más adelante tu situación cambia, lo puedes ajustar"), y un
-- listado con la misma persona tres veces no le sirve a quien lo lee.
--
-- El correo llega en el enlace del correo masivo y NO se verifica: no hay cuenta ni token. Es una
-- decisión de producto —lo que importa es el listado de respuestas, no ligarlas a la ficha del
-- médico—, así que esta tabla no tiene FK a `users`/`doctors` y no decide nada de acceso.
--
-- Solo la API accede (RLS deny-all: enable RLS sin policies; la API es owner y la bypasea).

create table if not exists public.marketing_survey_responses (
    id                  uuid primary key default gen_random_uuid(),

    -- 'psicologos' | 'especialistas' | 'medicos-generales' (ver ck_..._survey). Es el mismo slug
    -- de la página pública (/encuesta/<slug>) y del endpoint, para no mantener equivalencias.
    survey              text not null,

    -- Normalizado por la API (trim + minúsculas) antes de guardar: es la mitad de la clave única,
    -- y "Ana@x.com" y "ana@x.com " no pueden contar como dos personas.
    email               text not null,

    -- Opciones marcadas como CÓDIGOS estables ('atender_pacientes'), no con el texto del
    -- formulario: así el texto se puede reescribir sin invalidar lo ya respondido. Las etiquetas
    -- legibles las resuelve la API al listar y al exportar.
    roles               text[] not null default '{}',
    role_active_detail  text,  -- "Asumir un rol más activo": qué tiene en mente
    role_other_detail   text,  -- "Otra forma que quiero proponerles": su propuesta

    -- Disponibilidad. En médicos generales solo se pide si quiere atender o asumir un rol (quien
    -- solo va a pedir interconsultas no la contesta), así que puede quedar vacía. La regla vive
    -- en la API.
    moments             text[] not null default '{}',
    days                text[] not null default '{}',
    weekly_hours        text,
    availability_notes  text,

    -- Solo psicólogos y especialistas (pueden estar fuera de Venezuela): código de la zona
    -- elegida y, si eligió "Otra", la que escribió.
    timezone            text,
    timezone_other      text,

    notes               text,

    created_at          timestamptz not null default now(),  -- primera respuesta
    updated_at          timestamptz not null default now()   -- última vez que respondió
);

-- === Invariantes en la BD, no solo en Pydantic ===
do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'ck_marketing_survey_responses_survey'
                     and conrelid = 'public.marketing_survey_responses'::regclass) then
        alter table public.marketing_survey_responses
            add constraint ck_marketing_survey_responses_survey
            check (survey in ('psicologos', 'especialistas', 'medicos-generales'));
    end if;

    -- Las tres encuestas exigen marcar al menos una forma de participar: una respuesta sin
    -- ninguna no dice nada y solo ensucia el listado.
    if not exists (select 1 from pg_constraint
                   where conname = 'ck_marketing_survey_responses_roles'
                     and conrelid = 'public.marketing_survey_responses'::regclass) then
        alter table public.marketing_survey_responses
            add constraint ck_marketing_survey_responses_roles
            check (cardinality(roles) > 0);
    end if;
end $$;

-- === Índices ===

-- La clave del upsert (`on conflict (survey, email)`): una respuesta por persona y encuesta.
create unique index if not exists uq_marketing_survey_responses_survey_email
    on public.marketing_survey_responses (survey, email);

-- El listado del panel: una encuesta, las respuestas más recientes primero.
create index if not exists ix_marketing_survey_responses_survey_updated
    on public.marketing_survey_responses (survey, updated_at desc);

-- Solo la API lee y escribe esta tabla.
alter table public.marketing_survey_responses enable row level security;

comment on table public.marketing_survey_responses is
    'Respuestas a las encuestas de marketing (psicólogos, especialistas, médicos generales). Una '
    'fila por encuesta y correo; responder de nuevo la actualiza. El correo no está verificado.';
