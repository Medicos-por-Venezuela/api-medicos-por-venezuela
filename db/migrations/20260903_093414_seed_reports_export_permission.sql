-- Migración: seed reports export permission
-- Creada:    2026-09-03 09:34:14
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- Seed del permiso 'reports.export' (GET /reports/*) mapeado **solo a super_admin**.
--
-- A diferencia de 'stats.read', que se otorgó a admin y super_admin, este NO va a `admin`:
-- los reportes exportan la ficha completa de médicos y pacientes (cédulas, teléfonos,
-- alergias, descripción del caso) de miles de personas a un archivo que sale de la
-- plataforma. Es la operación con mayor exposición de PII de toda la API, y se restringe
-- al rol con responsabilidad última. Cada exportación queda además en `audit_log`.
--
-- Nota: el cross-join del seed original de RBAC (que dio TODOS los permisos a super_admin)
-- ya corrió; los permisos nuevos necesitan mapeo explícito, así que no basta con insertar
-- la fila en `permissions`.

-- === Permiso ===
insert into public.permissions (code, description)
select v.code, v.description
from (values
    (
        'reports.export',
        'Generar y exportar a Excel los reportes de médicos y pacientes (GET /reports/*)'
    )
) as v (code, description)
where not exists (select 1 from public.permissions p where p.code = v.code);

-- === Mapeo SOLO a super_admin (deliberadamente NO a admin) ===
insert into public.role_permissions (role_id, permission_id)
select r.id, p.id
from (values
    ('super_admin', 'reports.export')
) as m (role_code, perm_code)
join public.roles r on r.code = m.role_code and r.deleted_at is null
join public.permissions p on p.code = m.perm_code
where not exists (
    select 1 from public.role_permissions rp
    where rp.role_id = r.id and rp.permission_id = p.id
);
