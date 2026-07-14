-- Migración: backfill user_roles desde profiles.role
-- Asigna a cada perfil su rol actual en la tabla user_roles (coexistencia).
-- 'specialist' legado -> 'doctor'. assigned_by = null (asignación del sistema).
-- Idempotente: no duplica asignaciones activas.

insert into public.user_roles (user_id, role_id)
select p.id, r.id
from public.profiles p
join public.roles r
    on r.code = case when p.role = 'specialist' then 'doctor' else p.role end
   and r.deleted_at is null
where not exists (
    select 1 from public.user_roles ur
    where ur.user_id = p.id and ur.role_id = r.id and ur.revoked_at is null
);
