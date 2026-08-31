-- Migración: seed interconsultation requests permissions
-- Creada:    2026-08-31 17:44:44
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- Permisos de la interconsulta asíncrona (ver tasks/interconsulta-asincrona/spec.md).
-- Mismo patrón que 20260720_105744_seed_stats_read_permission.sql.
--
-- Dos permisos y no uno, porque son los dos lados del feature y no siempre irán juntos:
--   .write -> pedir ayuda: registrar el paciente, crear la solicitud, cancelarla y cerrarla.
--   .take  -> darla: ver la bandeja anonimizada y tomar un caso.
-- Separados, mañana se puede tener un médico que pide pero no atiende interconsultas (o al
-- revés) sin tocar código. Juntos, esa política exigiría un despliegue.
--
-- NO se reutiliza `queue.take`: ese es de la cola pública. Compartirlo daría acceso cruzado
-- entre dos flujos que no tienen nada que ver, y revocar uno revocaría el otro.
--
-- El cross-join del seed original de RBAC ya corrió, así que super_admin NO recibe los permisos
-- nuevos automáticamente: hay que mapearlo explícito, igual que se hizo con stats.read.

-- === Permisos ===
insert into public.permissions (code, description)
select v.code, v.description
from (values
    ('interconsultation_requests.write',
     'Pedir una interconsulta asíncrona: registrar paciente de consultorio, crear, cancelar y cerrar la solicitud'),
    ('interconsultation_requests.take',
     'Ver la bandeja de interconsultas de su especialidad y tomar un caso')
) as v (code, description)
where not exists (select 1 from public.permissions p where p.code = v.code);

-- === Mapeo a roles ===
-- `doctor` es el rol que usa el feature de punta a punta: todo médico puede pedir Y tomar.
-- admin/super_admin lo reciben para poder operar y diagnosticar sin cambiarse de cuenta.
insert into public.role_permissions (role_id, permission_id)
select r.id, p.id
from (values
    ('doctor',      'interconsultation_requests.write'),
    ('doctor',      'interconsultation_requests.take'),
    ('admin',       'interconsultation_requests.write'),
    ('admin',       'interconsultation_requests.take'),
    ('super_admin', 'interconsultation_requests.write'),
    ('super_admin', 'interconsultation_requests.take')
) as m (role_code, perm_code)
join public.roles r on r.code = m.role_code and r.deleted_at is null
join public.permissions p on p.code = m.perm_code
where not exists (
    select 1 from public.role_permissions rp
    where rp.role_id = r.id and rp.permission_id = p.id
);
