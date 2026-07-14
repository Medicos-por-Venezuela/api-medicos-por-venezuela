-- Migración: sync user_roles from profiles trigger
-- Creada:    2026-07-03 22:11:35
--
-- Espejo de coexistencia: refleja `profiles.role` -> `public.user_roles` cuando se
-- crea un usuario (tras el signup de Auth) o cuando cambia su rol. Mantenemos DOS
-- fuentes (profiles.role legacy y user_roles) hasta completar la migración a RBAC;
-- este trigger garantiza que un usuario nuevo obtenga su rol en la nueva tabla sin
-- pasar por la API (p. ej. si el perfil lo crea el trigger de Supabase Auth).
--
-- El trigger de `profiles` (handle_new_user en Auth) se conserva intacto.
--
-- Idempotente: create or replace + drop trigger if exists. La inserción respeta el
-- índice único parcial `uq_user_roles_active` (no duplica roles activos).

create or replace function public.sync_user_roles_from_profile()
returns trigger
language plpgsql
security definer          -- escribe en user_roles (RLS deny-all) sin depender del rol que dispare
set search_path = public
as $$
declare
    v_code    text;
    v_role_id uuid;
begin
    -- 'specialist' es legacy: todo especialista es 'doctor' en el nuevo modelo.
    v_code := case when new.role = 'specialist' then 'doctor' else new.role end;

    select id into v_role_id
    from public.roles
    where code = v_code and deleted_at is null;

    if v_role_id is null then
        return new;          -- rol sin equivalente (valor legacy desconocido): no-op seguro
    end if;

    -- Aditivo: agrega el rol si no lo tiene activo. NO revoca otros roles que un admin
    -- haya asignado por la API (multi-rol).
    -- ponytail: no sincroniza bajas (si profiles.role deja de ser X, no revoca X en user_roles).
    -- Techo aceptado durante la coexistencia; la revocación se hace por la API (roles.assign).
    insert into public.user_roles (user_id, role_id)   -- assigned_by null = sistema/trigger
    select new.id, v_role_id
    where not exists (
        select 1 from public.user_roles ur
        where ur.user_id = new.id
          and ur.role_id = v_role_id
          and ur.revoked_at is null
    );

    return new;
end;
$$;

drop trigger if exists trg_sync_user_roles_from_profile on public.profiles;
create trigger trg_sync_user_roles_from_profile
    after insert or update of role on public.profiles
    for each row
    execute function public.sync_user_roles_from_profile();
