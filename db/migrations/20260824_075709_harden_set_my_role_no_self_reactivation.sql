-- Migración: harden set my role no self reactivation
-- Creada:    2026-08-24 07:57:09
--
-- Elevación de privilegios (evasión de baneo) en la RPC `set_my_role`, gemela de
-- POST /api/v1/profiles/me/finalize-role. La versión anterior hacía:
--     set ... verified = true, active = true, role_chosen = true
--     where id = auth.uid() and role_chosen = false
-- `active` es el gate de acceso real (`current_user_role()` filtra por active, y
-- `Principal.has_permission` devuelve false si active=false), así que cualquier cuenta
-- con active=false y role_chosen=false podía REACTIVARSE SOLA, anulando la revocación de
-- un admin. El combo lo produce el trigger `handle_new_auth_user` para altas OAuth
-- (Google) sin metadata de rol: nacen con role_chosen=false; si luego un admin las
-- revoca, se recuperan con una sola llamada. La función es SECURITY DEFINER y sigue
-- concedida a `authenticated`, así que el revoke de escrituras sobre `public.users`
-- (20260802_104043) NO la cubría: es invocable vía PostgREST con el anon key + JWT.
--
-- Arreglo (mismo criterio que src/services/profiles.py::finalize_role): elegir rol fija
-- el rol y NADA MÁS. No toca active/verified —las cuentas ya nacen con ambos en true, el
-- set era redundante— y además exige `active = true` en el WHERE: una cuenta revocada no
-- finaliza nada (la RPC devuelve void, así que para ella es simplemente un no-op).
--
-- Idempotente: `create or replace function` + grant repetible. Transaccional.

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
    role_chosen = true
  where id = auth.uid() and role_chosen = false and active = true;
end;
$$;

grant execute on function public.set_my_role(text, text, text, text, text) to authenticated;
