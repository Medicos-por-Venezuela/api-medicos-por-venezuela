-- Migración: rename profiles to users with compat view
-- Creada:    2026-07-04 09:19:40
--
-- profiles -> users (fase EXPAND del expand/contract). Renombra la tabla de cuentas
-- y deja una VISTA `profiles` (mismo nombre de siempre) para que NO se caiga nada que
-- todavía nombre `profiles`: el frontend directo (anon key), el trigger de Auth
-- (handle_new_auth_user), el RPC set_my_role y las funciones RLS (current_user_role,
-- is_admin, is_staff, current_user_specialty).
--
-- El rename conserva FKs, índices, RLS y triggers (los sigue la tabla). La vista es
-- `select *` de una sola tabla -> auto-actualizable (INSERT/UPDATE/DELETE se reescriben
-- a users y disparan los triggers de users, incl. el de coexistencia de user_roles).
-- security_invoker=true (PG15+) hace que la RLS de users se aplique al consultar la vista.
--
-- Idempotente: guarda en el rename + create or replace view + grants repetibles.

-- 1) Rename de la tabla (solo si aplica). FKs/índices/RLS/triggers la siguen automáticamente.
do $$
begin
  if exists (
        select 1 from information_schema.tables
        where table_schema = 'public' and table_name = 'profiles' and table_type = 'BASE TABLE'
     ) and not exists (
        select 1 from information_schema.tables
        where table_schema = 'public' and table_name = 'users'
     ) then
    execute 'alter table public.profiles rename to users';
  end if;
end $$;

-- 2) Vista de compatibilidad con el nombre viejo. security_invoker => respeta la RLS de users.
create or replace view public.profiles with (security_invoker = true) as
  select * from public.users;

-- 3) Grants que la tabla tenía y la vista NO hereda (solo si los roles existen: en local/prod sí).
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant select, insert, update, delete on public.profiles to anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    grant select, insert, update, delete on public.profiles to authenticated;
  end if;
end $$;
