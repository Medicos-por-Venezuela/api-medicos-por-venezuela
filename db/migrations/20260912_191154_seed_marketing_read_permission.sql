-- Migración: seed marketing read permission
-- Creada:    2026-09-12 19:11:54
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- Seed del permiso 'marketing.read' (GET /marketing/surveys/*/responses y su exportación a
-- Excel) mapeado **solo a super_admin**, igual que 'reports.export'.
--
-- Mismo criterio que los reportes: el listado es la población completa de quienes respondieron,
-- con su correo, y se descarga a un archivo que sale de la plataforma. Se restringe al rol con
-- responsabilidad última; si el equipo de marketing trabaja con cuentas `admin`, basta otra
-- migración que añada el mapeo ('admin', 'marketing.read'), sin tocar código.
--
-- Permiso propio y no `reports.export`: compartirlo obligaría a dar acceso a las fichas completas
-- de pacientes a quien solo necesita ver quién respondió una encuesta.
--
-- El cross-join del seed original de RBAC ya corrió, así que super_admin NO recibe los permisos
-- nuevos automáticamente: hay que mapearlo explícito.

-- === Permiso ===
insert into public.permissions (code, description)
select v.code, v.description
from (values
    (
        'marketing.read',
        'Ver y exportar a Excel las respuestas de las encuestas de marketing (GET /marketing/*)'
    )
) as v (code, description)
where not exists (select 1 from public.permissions p where p.code = v.code);

-- === Mapeo SOLO a super_admin ===
insert into public.role_permissions (role_id, permission_id)
select r.id, p.id
from (values
    ('super_admin', 'marketing.read')
) as m (role_code, perm_code)
join public.roles r on r.code = m.role_code and r.deleted_at is null
join public.permissions p on p.code = m.perm_code
where not exists (
    select 1 from public.role_permissions rp
    where rp.role_id = r.id and rp.permission_id = p.id
);
