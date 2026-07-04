-- Migración: seed RBAC (roles del sistema, permisos y mapeo). Idempotente.

-- === Roles del sistema ===
insert into public.roles (code, name, is_system)
select v.code, v.name, true
from (values
    ('patient',     'Paciente'),
    ('doctor',      'Médico'),
    ('admin',       'Administrador'),
    ('super_admin', 'Super administrador')
) as v (code, name)
where not exists (select 1 from public.roles r where r.code = v.code and r.deleted_at is null);

-- === Permisos (catálogo inicial) ===
insert into public.permissions (code, description)
select v.code, v.description
from (values
    ('patients.read',       'Ver pacientes'),
    ('patients.write',      'Crear/editar pacientes'),
    ('patients.delete',     'Eliminar pacientes'),
    ('consultations.read',  'Ver consultas'),
    ('consultations.write', 'Crear/editar consultas'),
    ('consultations.close', 'Cerrar consultas / marcar no-show'),
    ('consultations.delete', 'Eliminar consultas'),
    ('queue.read',          'Ver la cola'),
    ('queue.take',          'Tomar/atender un caso de la cola'),
    ('queue.manage',        'Administrar la cola (liberar estancadas)'),
    ('doctors.read',        'Ver médicos'),
    ('doctors.write',       'Crear/editar médicos'),
    ('doctors.verify',      'Verificar credencial de médicos'),
    ('profiles.read',       'Ver perfiles'),
    ('profiles.manage',     'Activar/revocar perfiles'),
    ('roles.assign',        'Asignar/revocar roles a usuarios'),
    ('audit.read',          'Ver el registro de auditoría')
) as v (code, description)
where not exists (select 1 from public.permissions p where p.code = v.code);

-- === Mapeo rol -> permisos (doctor, specialist, admin) ===
insert into public.role_permissions (role_id, permission_id)
select r.id, p.id
from (values
    ('doctor', 'consultations.read'), ('doctor', 'consultations.write'),
    ('doctor', 'consultations.close'), ('doctor', 'queue.read'),
    ('doctor', 'queue.take'), ('doctor', 'patients.read'), ('doctor', 'doctors.read'),
    ('admin', 'patients.read'), ('admin', 'patients.write'), ('admin', 'patients.delete'),
    ('admin', 'consultations.read'), ('admin', 'consultations.write'),
    ('admin', 'consultations.close'), ('admin', 'consultations.delete'),
    ('admin', 'queue.read'), ('admin', 'queue.take'), ('admin', 'queue.manage'),
    ('admin', 'doctors.read'), ('admin', 'doctors.write'), ('admin', 'doctors.verify'),
    ('admin', 'profiles.read'), ('admin', 'profiles.manage'),
    ('admin', 'roles.assign'), ('admin', 'audit.read')
) as m (role_code, perm_code)
join public.roles r on r.code = m.role_code and r.deleted_at is null
join public.permissions p on p.code = m.perm_code
where not exists (
    select 1 from public.role_permissions rp
    where rp.role_id = r.id and rp.permission_id = p.id
);

-- === super_admin: TODOS los permisos ===
insert into public.role_permissions (role_id, permission_id)
select r.id, p.id
from public.roles r
cross join public.permissions p
where r.code = 'super_admin' and r.deleted_at is null
  and not exists (
    select 1 from public.role_permissions rp
    where rp.role_id = r.id and rp.permission_id = p.id
);

-- Nota: 'patient' queda sin permisos de staff a propósito; su acceso a lo propio
-- se resuelve por pertenencia (ownership), no por permisos RBAC.
