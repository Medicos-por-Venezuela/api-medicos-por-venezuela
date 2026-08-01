-- Migración: soft delete de pacientes. En este proyecto NO se hace hard delete: "borrar" un
-- paciente = marcar `deleted_at` y filtrarlo de las lecturas (trazabilidad, sin pérdida de datos).
-- Mismo patrón que doctors.deleted_at. Idempotente.

alter table public.patients
    add column if not exists deleted_at timestamptz;

-- Índice parcial: las listas filtran los NO borrados (el grueso), así que solo indexamos esos.
create index if not exists ix_patients_not_deleted
    on public.patients (created_at) where deleted_at is null;

-- Ya no existe el hard delete, así que sobra el permiso `patients.delete`: el archivar (soft
-- delete) se gatea con `patients.write`, igual que la baja lógica de doctors (que usa doctors.write).
-- Se elimina el permiso y sus asignaciones de rol. Idempotente.
delete from public.role_permissions
where permission_id = (select id from public.permissions where code = 'patients.delete');

delete from public.permissions where code = 'patients.delete';
