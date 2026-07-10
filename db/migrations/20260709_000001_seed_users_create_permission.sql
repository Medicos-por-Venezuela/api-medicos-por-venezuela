-- Migración: seed del permiso 'users.create' (creación de usuarios de Auth vía
-- POST /users) y su mapeo a admin/super_admin. Idempotente (mismo patrón que
-- 20260703_161550_seed_rbac_roles_and_permissions.sql).
--
-- Nota de acoplamiento (documentado también en design.md): create_user() no
-- re-verifica 'roles.assign' de forma independiente al delegar en assign_role();
-- se apoya en que este seed mapea 'users.create' a los MISMOS roles
-- ('admin', 'super_admin') que ya tienen 'roles.assign'. Si algún día estos dos
-- permisos se otorgan a roles distintos, revisar esa asunción en
-- src/services/users.py::create_user.

-- === Permiso ===
insert into public.permissions (code, description)
select v.code, v.description
from (values
    ('users.create', 'Crear usuarios de Auth (POST /users)')
) as v (code, description)
where not exists (select 1 from public.permissions p where p.code = v.code);

-- === Mapeo a admin y super_admin ===
insert into public.role_permissions (role_id, permission_id)
select r.id, p.id
from (values
    ('admin', 'users.create'),
    ('super_admin', 'users.create')
) as m (role_code, perm_code)
join public.roles r on r.code = m.role_code and r.deleted_at is null
join public.permissions p on p.code = m.perm_code
where not exists (
    select 1 from public.role_permissions rp
    where rp.role_id = r.id and rp.permission_id = p.id
);
