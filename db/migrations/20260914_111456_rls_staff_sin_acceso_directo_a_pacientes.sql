-- Migración: rls staff sin acceso directo a pacientes
-- Creada:    2026-09-14 11:14:56
--
-- Hallazgo (2026-09-14): la API ya aplica "cada médico ve solo sus pacientes; de la cola, solo
-- la descripción", pero la RLS de Supabase NO. `patients_select_staff` y
-- `consultations_select_staff` dejaban leer TODAS las filas a cualquier cuenta con
-- `users.role = 'doctor'`, y `current_user_role()` solo mira `users.role/active/verified`:
-- no le pide ficha en `doctors`. Con el anon key (público) y su propio JWT, una cuenta de Auth
-- sin ficha —un alta de médico que se quedó a medias— podía leer por PostgREST la cédula, el
-- teléfono y el motivo de consulta de todos los pacientes. En producción eso eran 2869 de 2967
-- cuentas con rol médico que la API ya bloquea (sin ficha, sin cédula, sin licencia o sin
-- verificar) pero la base no.
--
-- El frontend (`dev_aws`) ya no lee ninguna tabla por PostgREST: todo va por la API. Lo único
-- directo que queda es Realtime `postgres_changes` sobre `consultations`, que el panel usa
-- como SEÑAL para volver a pedir la cola a la API (panel-medico.tsx) y para ver el `status`
-- de la consulta abierta (consulta/[id].tsx). Así que:
--
-- 1) `current_user_role()` exige, a quien ejerce como médico, lo mismo que el gate de la API
--    (`doctors.has_valid_credential`): ficha viva, activa, verificada, con cédula y licencia.
--    Un admin por RBAC no depende de su ficha, igual que en `get_current_principal`.
-- 2) `patients` y `consultation_events`: sin policies ni SELECT para `authenticated`. Nadie los
--    lee directo; el médico ve a SUS pacientes por la API, que valida la pertenencia.
-- 3) `consultations_select_own` (el paciente ve sus consultas) deja de leer `patients` con los
--    privilegios del que consulta: la pertenencia pasa a una función security definer.
-- 4) `consultations`: `authenticated` conserva SELECT solo en `id`, `status` y
--    `assigned_doctor_id` — lo que Realtime necesita para avisar. Realtime respeta privilegios
--    por COLUMNA (`realtime.apply_rls` y `subscription_check_filters` usan
--    `has_column_privilege`), así que la señal sigue llegando sin el motivo, las notas ni la
--    sala de video.
--
-- Idempotente: create or replace, drop policy if exists, y revoke/grant repetibles. Los guards
-- de pg_roles replican el patrón de las migraciones de GRANT (un Postgres sin Supabase no tiene
-- `authenticated`).

-- 1) Criterio de credencial en SQL. Espejo de `services.doctors._blocked_reason`: si uno cambia,
--    cambia el otro (lo fija tests/test_rls_policies.py::test_doctor_can_practice_igual_que_la_api).
create or replace function public.doctor_can_practice(p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.doctors d
    where d.user_id = p_user_id
      and d.deleted_at is null
      and d.status = 1
      and coalesce(btrim(d.cedula), '') <> ''
      and coalesce(btrim(d.license), '') <> ''
      and d.verified
  );
$$;

-- Solo para uso interno de las funciones de RLS: expuesta, cualquiera podría preguntar por PostgREST
-- (/rpc) si un user_id ajeno está habilitado para atender.
revoke all on function public.doctor_can_practice(uuid) from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function public.doctor_can_practice(uuid) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on function public.doctor_can_practice(uuid) from authenticated;
  end if;
end $$;

create or replace function public.current_user_role()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select u.role
  from public.users u
  where u.id = auth.uid()
    and u.active = true
    and u.verified = true
    and (
      u.role not in ('doctor', 'specialist')
      or public.doctor_can_practice(u.id)
      or exists (
        select 1
        from public.user_roles ur
        join public.roles r on r.id = ur.role_id
        where ur.user_id = u.id
          and ur.revoked_at is null
          and r.deleted_at is null
          and r.code in ('admin', 'super_admin')
      )
    );
$$;

-- 2) `patients` y `consultation_events`: nadie los lee por PostgREST. Fuera las policies Y el
--    privilegio: una policy sin GRANT es código muerto que aparenta un acceso que no hay, y un
--    GRANT sin policy es un acceso a un paso de que alguien añada una. Los GRANTs de estas tablas
--    ni siquiera coinciden entre producción y el Supabase local (prod tenía SELECT, local no), así
--    que esta migración fija el estado en vez de depender del que hubiera.
drop policy if exists patients_select_staff on public.patients;
drop policy if exists patients_select_own on public.patients;
drop policy if exists events_select_staff on public.consultation_events;
drop policy if exists events_insert_staff on public.consultation_events;

-- 3) `consultations`: el paciente con cuenta sigue viendo LO SUYO, pero sin leer `patients`.
--    La policy anterior hacía `exists (select … from patients)` con los privilegios de quien
--    consulta, así que dependía del SELECT sobre `patients` que se quita arriba — y como las
--    policies se combinan con OR, sin ese privilegio fallaba la lectura de TODOS, médicos
--    incluidos. La pertenencia se resuelve ahora en una función security definer.
create or replace function public.owns_patient(p_patient_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.patients p
    where p.id = p_patient_id
      and p.user_id = auth.uid()
      and p.deleted_at is null
  );
$$;

revoke all on function public.owns_patient(uuid) from public;

drop policy if exists consultations_select_own on public.consultations;
create policy consultations_select_own
on public.consultations
for select
to authenticated
using (public.owns_patient(patient_id));

-- 4) Privilegios: solo las columnas de la señal de Realtime en `consultations`, nada en las otras.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function public.owns_patient(uuid) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    -- Quien evalúa la policy es el que consulta: necesita poder ejecutarla. Solo responde por
    -- pacientes propios, así que no revela nada ajeno.
    grant execute on function public.owns_patient(uuid) to authenticated;
    revoke select on public.patients, public.consultation_events from authenticated;
    revoke select on public.consultations from authenticated;
    grant select (id, status, assigned_doctor_id) on public.consultations to authenticated;
  end if;
end $$;
