-- Migración: grant service_role on patients and consultations
-- Creada:    2026-07-05 19:05:20
--
-- Mismo gap que las 2 migraciones de grant anteriores (anon/authenticated en users,
-- patients/consultations/consultation_events): nunca hubo un GRANT explícito
-- versionado para `service_role`. En prod "funciona" porque la plataforma Supabase
-- lo concede por defecto a todas las tablas nuevas (mismo comportamiento legacy
-- `auto_expose_new_tables` que se retira 2026-10-30); localmente (CLI fresco) no.
--
-- Lo dispara el frontend (medicos-por-venezuela/lib/supabaseAdmin.ts, usado SOLO por
-- pages/api/videoconsulta.ts) para leer la consulta + teléfono del paciente y guardar
-- la sala de Jitsi — vía REST (PostgREST), no esta API. `service_role` en Supabase
-- tiene BYPASSRLS, pero el GRANT de tabla es una capa independiente de RLS y sigue
-- siendo obligatorio.

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select, update on public.consultations to service_role;
    grant select on public.patients to service_role;
  end if;
end $$;

