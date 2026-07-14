-- Migración: grant anon authenticated on users table
-- Creada:    2026-07-05 10:45:58
--
-- Gap del rename profiles->users (20260704_091940): esa migración solo otorgó GRANT
-- sobre la VISTA `profiles`, nunca sobre la tabla base `users`. Con
-- security_invoker=true (la vista corre con los permisos del rol que consulta, no del
-- dueño), PostgREST necesita el grant en AMBAS: la vista Y la tabla base. Sin esto,
-- consultar por `/rest/v1/profiles` falla con "permission denied for table users"
-- aunque la vista sí tenga el grant.
--
-- RLS de `users` ya sigue correcta desde el rename (las políticas se mueven con la
-- tabla); esto solo agrega el grant de nivel-tabla que faltaba.

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant select, insert, update, delete on public.users to anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    grant select, insert, update, delete on public.users to authenticated;
  end if;
end $$;
