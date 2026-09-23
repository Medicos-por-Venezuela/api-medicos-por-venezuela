-- Migración: rls tablas clinicas deny all
-- Creada:    2026-09-23 13:49:11
--
-- Contexto: el contenido clínico (motivo, notas, antecedentes, récipes…) pasa a guardarse
-- cifrado por la API (AES-256-GCM, clave solo en el entorno de la API; ver
-- src/core/clinical_crypto.py y docs/cifrado-datos-clinicos.md). Esta migración cierra lo que
-- quedaba abierto a nivel de base para que el aislamiento no dependa solo del cifrado:
--
-- 1) `treatment_plans` y `messages` arrastraban policies del esquema original con
--    `USING (true)` para el rol `public` (lectura y escritura para anon incluido). Hoy están
--    vacías y la API no las usa, pero con los GRANT por defecto de Supabase cualquiera con el
--    anon key podía leerlas o insertarles filas por PostgREST.
-- 2) Todas las tablas clínicas quedan deny-all para `anon`/`authenticated`: RLS activa, sin
--    policies y sin privilegios. La API entra como dueña y es la única que las lee.
--    Incluye `TRUNCATE`, que ignora la RLS por completo (no hay policy que la frene).
-- 3) `consultations` y `patients` tenían policies de UPDATE sin GRANT de UPDATE detrás
--    (se revocó en 20260802_104043): código muerto que aparenta un acceso que no hay. Fuera.
--    `consultations` conserva SOLO lo que Realtime necesita (SELECT de id, status y
--    assigned_doctor_id, 20260914_111456): la metadata administrativa, nunca el payload clínico.
--    Esa separación por columnas es la "vista administrativa" de la base; no se crea una vista
--    nueva porque nada debe leer consultas por PostgREST (regla de .claude/rules/security.md).
-- 4) Índice para responder "¿quién leyó el caso X?" sobre las entradas READ_CLINICAL_DATA,
--    que guardan los ids leídos en metadata.ids (un listado escribe una sola fila).
--
-- Idempotente: drop policy if exists, revoke repetibles, create index if not exists. Los guards
-- de pg_roles replican el patrón de las migraciones de GRANT (un Postgres sin Supabase no tiene
-- `anon`/`authenticated`).

-- 1) Policies abiertas del esquema original.
drop policy if exists "public select messages" on public.messages;
drop policy if exists "public insert messages" on public.messages;
drop policy if exists "public all treatment_plans" on public.treatment_plans;
drop policy if exists "public select treatment_plans" on public.treatment_plans;
drop policy if exists "public insert treatment_plans" on public.treatment_plans;
drop policy if exists "public update treatment_plans" on public.treatment_plans;

-- 3) Policies de UPDATE sin privilegio detrás.
drop policy if exists consultations_update_admin on public.consultations;
drop policy if exists consultations_update_staff on public.consultations;
drop policy if exists patients_update_admin on public.patients;

-- 2) RLS activa en todas (no-op si ya lo estaba) y fuera cualquier privilegio de los roles
--    del navegador.
alter table public.prescriptions enable row level security;
alter table public.referrals enable row level security;
alter table public.rest_notes enable row level security;
alter table public.treatment_plans enable row level security;
alter table public.follow_ups enable row level security;
alter table public.messages enable row level security;
alter table public.interconsultations enable row level security;
alter table public.interconsultation_requests enable row level security;
alter table public.consultation_events enable row level security;

do $$
declare
  r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format(
        'revoke all on public.prescriptions, public.referrals, public.rest_notes, '
        'public.treatment_plans, public.follow_ups, public.messages, '
        'public.interconsultations, public.interconsultation_requests, '
        'public.consultation_events from %I',
        r
      );
      -- consultations/patients: sin escritura ni TRUNCATE. SELECT NO va en la lista a propósito:
      -- en Postgres, revocar un privilegio de tabla lo revoca también en todas sus columnas, y
      -- el SELECT por columnas de consultations (id, status, assigned_doctor_id) es la señal
      -- de Realtime del panel. Quitarlo aquí rompería el panel sin error visible.
      execute format(
        'revoke insert, update, delete, truncate, references, trigger '
        'on public.consultations, public.patients from %I',
        r
      );
    end if;
  end loop;
end $$;

-- 4) Trazabilidad de lecturas clínicas.
create index if not exists audit_log_clinical_read_ids_idx
  on public.audit_log using gin ((metadata -> 'ids'))
  where action = 'READ_CLINICAL_DATA';

comment on column public.consultations.chief_complaint is
  'Cifrado por la API (enc:v1:<kid>:<b64>, AES-256-GCM). Ilegible sin CLINICAL_DATA_ENCRYPTION_KEY.';
comment on column public.consultations.internal_note is
  'Cifrado por la API (enc:v1). Solo equipo tratante; nunca admin ni paciente.';
comment on column public.consultations.clinical_notes is
  'Cifrado por la API (enc:v1). Solo equipo tratante; nunca admin ni paciente.';
comment on column public.patients.description is
  'Antecedentes. Cifrado por la API (enc:v1).';
comment on column public.patients.allergies is
  'Cifrado por la API (enc:v1).';
