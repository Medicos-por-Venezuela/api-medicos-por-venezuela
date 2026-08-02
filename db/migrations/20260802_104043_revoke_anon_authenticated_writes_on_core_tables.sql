-- Migración: revoke anon authenticated writes on core tables
-- Creada:    2026-08-02 10:40:43
--
-- Hallazgo M2 de la auditoría. Las migraciones 20260704_091940, 20260705_104558 y
-- 20260705_105606 otorgaron a `anon` y `authenticated` permisos de tabla directos sobre
-- users/profiles/patients/consultations/consultation_events. Hoy lo contiene la RLS, así
-- que no escala privilegios, pero deja DOS policies que permiten a un anónimo INSERTAR
-- pacientes y consultas directo por PostgREST con el anon key (que es público por diseño).
--
-- Eso vacía de sentido el rate limiting del backend (hallazgo A1): un atacante no llama a
-- la API, escribe directo en PostgREST y llena la cola igual. A1 sin esto es media solución.
--
-- ORDEN DE APLICACIÓN — NO aplicar hasta que el frontend migrado esté en producción.
-- La versión vieja (rama `main` del frontend) escribe Supabase directo: `admin/pacientes`
-- (16 accesos), `consulta/[id]` (7), `mi-caso` (3) y la RPC `mark_patient_waiting`. Aplicar
-- esto con esa versión viva rompe el registro de pacientes y el panel. El frontend nuevo
-- (`dev_aws`) tiene CERO accesos directos a tablas: solo Auth y canales Realtime.
--
-- NO se revoca el SELECT de `authenticated` sobre `consultations`: el panel médico lo lee
-- en vivo por Realtime `postgres_changes` (pages/panel-medico.tsx, consulta/[id].tsx) y
-- Realtime respeta GRANTs y RLS. Revocarlo deja la cola sin actualización en tiempo real.
-- Los canales de *presence* no dependen de GRANTs, así que no se ven afectados.
--
-- Verificación tras aplicar: los e2e `presence` y `panel-race` son los que prueban que
-- Realtime sobrevivió.
--
-- Idempotente: REVOKE sobre un privilegio ya revocado es un no-op, y DROP POLICY lleva
-- IF EXISTS. Los guards de pg_roles/pg_views replican el patrón de las migraciones de GRANT.

do $$
begin
  -- === Escritura: fuera para ambos roles en las cinco relaciones ===
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke insert, update, delete on public.users from anon;
    revoke insert, update on public.patients, public.consultations from anon;
    revoke insert on public.consultation_events from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke insert, update, delete on public.users from authenticated;
    revoke insert, update on public.patients, public.consultations from authenticated;
    revoke insert on public.consultation_events from authenticated;
  end if;

  -- TRUNCATE: no lo otorgó este repo, viene de los default privileges de Supabase
  -- (pg_default_acl da `Dxtm` a anon/authenticated en toda tabla creada por `postgres`).
  -- No es explotable por PostgREST (no expone TRUNCATE), pero revocar DELETE y dejar
  -- TRUNCATE es incoherente: DELETE lo filtra la RLS, TRUNCATE la ignora por completo y
  -- vacía la tabla entera. Se quita en las cinco que guardan la PII.
  -- OJO: esto NO cambia el default, así que una tabla NUEVA vuelve a nacer con TRUNCATE
  -- para anon. Arreglarlo de raíz es `alter default privileges`, decisión aparte.
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke truncate on public.users, public.patients, public.consultations,
                       public.consultation_events from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke truncate on public.users, public.patients, public.consultations,
                       public.consultation_events from authenticated;
  end if;

  -- La vista de compatibilidad `profiles` tiene sus PROPIOS grants (20260704_091940:39,42).
  -- Con security_invoker=true el permiso se evalúa contra `users`, así que el revoke de
  -- arriba ya bloquea la escritura por la vista; se revoca igual para que los privilegios
  -- no mientan y para no dejar una mina si alguien cambia esa opción.
  if exists (select 1 from pg_views where schemaname = 'public' and viewname = 'profiles') then
    if exists (select 1 from pg_roles where rolname = 'anon') then
      revoke insert, update, delete, truncate on public.profiles from anon;
      revoke select on public.profiles from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      revoke insert, update, delete, truncate on public.profiles from authenticated;
    end if;
  end if;

  -- === Lectura: `anon` no tiene por qué leer NADA de estas tablas ===
  -- (a `authenticated` se le conserva el select; la RLS sigue acotando qué ve.)
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke select on public.users, public.patients, public.consultations,
                     public.consultation_events from anon;
  end if;
end $$;

-- === Las dos policies que permitían el alta anónima directa por PostgREST ===
-- El alta de paciente/consulta pasa ahora por el backend (POST /patients, /consultations),
-- que sí aplica rate limit y validación.
drop policy if exists patients_insert_public on public.patients;
drop policy if exists consultations_insert_public on public.consultations;
