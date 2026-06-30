-- Stubs mínimos de Supabase para poder restaurar el dump de producción en un
-- Postgres local "pelado". En Supabase estos objetos los provee la plataforma
-- (esquema auth, función auth.uid(), roles anon/authenticated). En local solo
-- necesitamos que existan para que el esquema (FKs, defaults, políticas RLS) se
-- cree sin errores. NO replican la lógica de autenticación real.

-- Roles a los que apuntan las políticas RLS del dump.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
end
$$;

-- Esquema auth + tabla mínima referenciada por las FKs (profiles.id,
-- patients.user_id, doctors.user_id -> auth.users.id).
create schema if not exists auth;

create table if not exists auth.users (
  id uuid primary key
);

-- auth.uid() es usada por funciones, defaults y políticas. En local devuelve NULL.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select null::uuid;
$$;

create or replace function auth.role()
returns text
language sql
stable
as $$
  select null::text;
$$;
