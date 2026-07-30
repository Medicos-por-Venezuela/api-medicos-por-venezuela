-- Migración: point db functions to users table (drop dependency on profiles view)
-- Creada:    2026-07-21 19:43:34
--
-- Fase CONTRACT (paso 1) del expand/contract profiles->users (ver
-- docs/migracion-profiles-a-users.md §9). Tras el rename, `public.profiles` quedó
-- como VISTA de compatibilidad sobre `public.users`. El backend (ORM) ya apunta a
-- `users`; los ÚNICOS consumidores que todavía nombran la vista son:
--   (a) las funciones/trigger de la BD definidas en 000_core_schema, y
--   (b) el frontend directo (anon key) — se migra aparte en el repo del front.
--
-- Esta migración elimina la dependencia (a): reescribe cada función SECURITY DEFINER
-- para que lea/escriba `public.users` DIRECTAMENTE en vez de pasar por la vista. Es un
-- no-op semántico (la vista es `select *` de una sola tabla auto-actualizable: misma
-- tabla física, mismos triggers —incl. el de coexistencia trg_sync_user_roles_from_profile,
-- que tras el rename vive en `users`—, mismos datos). Solo cambia el nombre referenciado.
--
-- NO se dropea la vista `public.profiles` aquí: el frontend todavía la usa. El drop es
-- el paso final, DESPUÉS de que el front deje de hacer `.from('profiles')`.
--
-- Idempotente: todo es `create or replace function` + grants repetibles. Transaccional.

-- 1) Trigger de Auth: crea la fila de cuenta al alta de un usuario de Supabase Auth.
create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  meta_role text := new.raw_user_meta_data->>'role';
  resolved_role text;
  has_role boolean;
begin
  -- coalesce: NULL metadata (e.g. Google sign-in) must yield false, not NULL,
  -- because role_chosen is NOT NULL.
  has_role := coalesce(meta_role in ('patient', 'doctor'), false);
  resolved_role := case when has_role then meta_role else 'patient' end;

  insert into public.users (
    id, email, full_name, role,
    specialty, country, medical_license, whatsapp_number,
    verified, active, role_chosen
  )
  values (
    new.id,
    new.email,
    coalesce(
      new.raw_user_meta_data->>'full_name',
      new.raw_user_meta_data->>'name',
      split_part(new.email, '@', 1)
    ),
    resolved_role,
    case when resolved_role = 'doctor' then new.raw_user_meta_data->>'specialty' end,
    case when resolved_role = 'doctor' then new.raw_user_meta_data->>'country' end,
    case when resolved_role = 'doctor' then new.raw_user_meta_data->>'medical_license' end,
    case when resolved_role = 'doctor' then new.raw_user_meta_data->>'whatsapp_number' end,
    true,
    true,
    has_role -- email signup with an explicit role is finalized; OAuth placeholders are not
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

-- 2) RPC set_my_role: el usuario finaliza su PROPIO rol una sola vez (/elegir-rol).
create or replace function public.set_my_role(
  p_role text,
  p_specialty text default null,
  p_country text default null,
  p_medical_license text default null,
  p_whatsapp_number text default null
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_role not in ('patient', 'doctor') then
    raise exception 'invalid role';
  end if;

  update public.users
  set
    role = p_role,
    specialty = case when p_role = 'doctor' then p_specialty else specialty end,
    country = case when p_role = 'doctor' then p_country else country end,
    medical_license = case when p_role = 'doctor' then p_medical_license else medical_license end,
    whatsapp_number = case when p_role = 'doctor' then p_whatsapp_number else whatsapp_number end,
    verified = true,
    active = true,
    role_chosen = true
  where id = auth.uid() and role_chosen = false;
end;
$$;

grant execute on function public.set_my_role(text, text, text, text, text) to authenticated;

-- 3) Helper de RLS: rol efectivo del usuario autenticado (lo usan is_admin/is_staff).
create or replace function public.current_user_role()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select role from public.users where id = auth.uid() and active = true and verified = true;
$$;

-- 4) RPC de presencia: el médico marca su propio heartbeat.
create or replace function public.mark_myself_online()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.users set last_seen_at = now() where id = auth.uid();
end;
$$;

grant execute on function public.mark_myself_online() to authenticated;

-- 5) admin_delete_patient: la guarda de super_admin lee la cuenta directamente de users.
create or replace function public.admin_delete_patient(p_patient_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if not exists (
    select 1 from public.users
    where id = auth.uid() and role = 'super_admin' and active = true
  ) then
    raise exception 'Only an active super_admin may delete patients';
  end if;

  delete from public.consultation_events
    where consultation_id in (select id from public.consultations where patient_id = p_patient_id);
  delete from public.consultations where patient_id = p_patient_id;
  delete from public.patients where id = p_patient_id;
end;
$$;

grant execute on function public.admin_delete_patient(uuid) to authenticated;
