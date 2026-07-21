-- Migración: seed stats read permission
-- Creada:    2026-07-20 10:57:44
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- Seed del permiso 'stats.read' (GET /stats/dashboard) y su mapeo a admin/super_admin.
-- Idempotente (mismo patrón que 20260709_000001_seed_users_create_permission.sql).
-- super_admin no lo recibe automáticamente por el cross-join del seed original
-- (ese cross-join ya corrió; los permisos nuevos necesitan mapeo explícito), así
-- que se otorga a ambos roles de forma explícita, igual que users.create.

-- === Permiso ===
insert into public.permissions (code, description)
select v.code, v.description
from (values
    ('stats.read', 'Ver métricas del dashboard (GET /stats/dashboard)')
) as v (code, description)
where not exists (select 1 from public.permissions p where p.code = v.code);

-- === Mapeo a admin y super_admin ===
insert into public.role_permissions (role_id, permission_id)
select r.id, p.id
from (values
    ('admin', 'stats.read'),
    ('super_admin', 'stats.read')
) as m (role_code, perm_code)
join public.roles r on r.code = m.role_code and r.deleted_at is null
join public.permissions p on p.code = m.perm_code
where not exists (
    select 1 from public.role_permissions rp
    where rp.role_id = r.id and rp.permission_id = p.id
);
