-- Migración: point remaining functions to users and drop profiles view
-- Creada:    2026-08-02 10:51:08
--
-- Cierra el rename profiles -> users. La migración 20260721_194334 repuntó las funciones de
-- seguridad (current_user_role, is_admin, is_staff, ...), pero se dejó TRES que siguen
-- consultando la vista de compatibilidad `public.profiles`:
--
--   update_my_specialty      update public.profiles ...
--   current_user_specialty   select specialty from public.profiles ...
--   get_case_doctor          join public.profiles pr ...
--
-- Postgres NO rastrea estas dependencias (el cuerpo de una función no se analiza), así que
-- un `drop view profiles` habría pasado sin error y las tres habrían reventado en runtime,
-- solo al llamarlas. De ahí que se repunten ANTES de borrar la vista, en esta misma
-- transacción.
--
-- Las tres son SECURITY DEFINER y corren como owner, así que no dependen de los GRANTs que
-- revoca 20260802_104043; el cambio es puramente de nombre de relación.
--
-- ORDEN DE APLICACIÓN — mismo gate que 20260802_104043: NO aplicar hasta que el frontend
-- migrado esté en producción. La rama `main` del frontend lee y escribe `profiles` por
-- nombre (auth callback, elegir-rol, revocar acceso, admin). El frontend nuevo (`dev_aws`)
-- solo la menciona en comentarios de "ya no se usa". El backend ya mapea a `users`
-- (src/models/profile.py: __tablename__ = "users"), así que no le afecta.
--
-- Idempotente: CREATE OR REPLACE FUNCTION y DROP VIEW IF EXISTS.

-- 1) Repuntar las tres funciones a public.users (mismo cuerpo, misma firma, misma
--    volatilidad y search_path; solo cambia la relación).

create or replace function public.update_my_specialty(p_specialty text)
returns void
language plpgsql
security definer
set search_path to 'public'
as $function$
begin
  if coalesce(btrim(p_specialty), '') = '' then
    raise exception 'La especialidad no puede estar vacía';
  end if;
  update public.users
  set specialty = btrim(p_specialty)
  where id = auth.uid()
    and role in ('doctor', 'specialist');
end;
$function$;

create or replace function public.current_user_specialty()
returns text
language sql
stable security definer
set search_path to 'public'
as $function$
  select specialty from public.users where id = auth.uid();
$function$;

create or replace function public.get_case_doctor(p_consultation_id uuid)
returns table(full_name text, specialty text)
language sql
stable security definer
set search_path to 'public'
as $function$
  select u.full_name, u.specialty
  from public.consultations c
  join public.patients pt on pt.id = c.patient_id
  join public.users u on u.id = c.assigned_doctor_id
  where c.id = p_consultation_id
    and pt.user_id = auth.uid();
$function$;

-- 2) Ya sin dependencias: fuera la vista de compatibilidad.
--    RESTRICT (el default) es deliberado: si algo la referencia por dependencia real
--    (otra vista), preferimos que esto falle en la migración y no en producción.
drop view if exists public.profiles;
