-- Migración: patients alta por medico
-- Creada:    2026-08-31 17:00:51
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- Abre una SEGUNDA vía de alta de pacientes: la del médico que registra a un paciente de su
-- consultorio para pedir una interconsulta (ver tasks/interconsulta-asincrona/spec.md). Hasta
-- ahora `patients` solo se llenaba por el alta PÚBLICA (el paciente se registra solo, entra a
-- la cola). Las dos vías conviven en la misma tabla y se distinguen por `created_by_doctor_id`.

-- === 1. Dueño del registro ===
-- NULL = alta pública (todo lo que existe hoy). NOT NULL = lo registró ese médico y es suyo:
-- la pertenencia se valida en la capa de servicio (RLS no aplica, la API entra como dueño).
-- `on delete set null` igual que doctors.user_id: si se borra la cuenta del médico, el paciente
-- queda huérfano pero no se pierde (misma lógica que el soft delete de pacientes).
alter table public.patients
    add column if not exists created_by_doctor_id uuid
        references public.users (id) on delete set null;

create index if not exists ix_patients_created_by_doctor
    on public.patients (created_by_doctor_id)
    where created_by_doctor_id is not null;

comment on column public.patients.created_by_doctor_id is
    'Médico que registró a este paciente de su consultorio (users.id). NULL = alta pública '
    'del propio paciente. Marca cuál de las dos vías de alta creó la fila.';

-- === 2. Campos de la cola dejan de ser obligatorios (solo para la vía del médico) ===
-- `phone_whatsapp` y `affected_zone` existen para la COLA: contactar al paciente y ubicarlo
-- geográficamente. Un paciente de consultorio no entra a la cola y el especialista nunca lo va
-- a contactar (habla con el médico tratante), así que pedirle esos datos al médico sería
-- fricción por nada — y además PII que no necesitamos guardar.
alter table public.patients alter column phone_whatsapp drop not null;
alter table public.patients alter column affected_zone  drop not null;

-- ...pero el alta PÚBLICA los sigue exigiendo. En vez de perder la garantía al quitar el NOT
-- NULL, se traslada a un CHECK condicionado a la vía de alta: sin médico dueño, ambos siguen
-- siendo obligatorios. Así la cola no puede recibir una fila sin teléfono ni zona.
--
-- Postgres no tiene ADD CONSTRAINT IF NOT EXISTS, de ahí el DO block (idempotencia).
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'ck_patients_contacto_requerido_en_alta_publica'
          and conrelid = 'public.patients'::regclass
    ) then
        alter table public.patients
            add constraint ck_patients_contacto_requerido_en_alta_publica
            check (
                created_by_doctor_id is not null
                or (phone_whatsapp is not null and affected_zone is not null)
            );
    end if;
end $$;

-- Las filas existentes son todas de alta pública y traen ambos campos (el NOT NULL lo garantizó
-- hasta ahora), así que el CHECK se valida sin fallar y no hace falta backfill.

comment on constraint ck_patients_contacto_requerido_en_alta_publica on public.patients is
    'El alta pública (created_by_doctor_id NULL) sigue exigiendo teléfono y zona afectada: son '
    'los datos con los que opera la cola. El alta por médico está exenta.';
