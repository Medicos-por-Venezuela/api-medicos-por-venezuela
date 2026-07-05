-- Migración: grant anon authenticated on core public tables
-- Creada:    2026-07-05 10:56:06
--
-- Gap heredado de Supabase: `patients`/`consultations`/`consultation_events` tienen
-- políticas RLS (creadas en supabase_schema.sql del frontend) pero NUNCA tuvieron un
-- GRANT de tabla explícito en ningún .sql versionado — en prod funcionaba porque la
-- plataforma auto-exponía tablas nuevas a anon/authenticated (comportamiento legacy,
-- invisible, no capturado por `pg_dump --no-privileges`). Supabase anuncia que ese
-- auto-expose se **elimina el 2026-10-30** (ver `auto_expose_new_tables` en
-- supabase/config.toml) — sin este GRANT explícito, esto rompería también en prod
-- cuando la plataforma saque el comportamiento legacy, no solo en local.
--
-- Privilegios al mínimo que las políticas existentes necesitan (sin DELETE: no hay
-- política de borrado en ninguna de estas tres; el borrado de paciente es vía la
-- función security definer `admin_delete_patient`, que no depende de GRANTs).

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant select, insert, update on public.patients to anon;
    grant select, insert, update on public.consultations to anon;
    grant select, insert on public.consultation_events to anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    grant select, insert, update on public.patients to authenticated;
    grant select, insert, update on public.consultations to authenticated;
    grant select, insert on public.consultation_events to authenticated;
  end if;
end $$;
