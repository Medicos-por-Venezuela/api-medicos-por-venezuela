-- Migración: convertir admin_delete_patient de HARD delete a SOFT delete.
-- En este proyecto NO se borra nada de la BD. Esta RPC (security definer, invocada directo por el
-- frontend admin) borraba en cascada events + consultations + el paciente. Se reescribe para solo
-- marcar patients.deleted_at (las lecturas ya lo filtran). Las consultas/events quedan para
-- trazabilidad. Se conserva la guarda de super_admin. El frontend sigue funcionando sin cambios;
-- cuando su PR migre al endpoint del backend (DELETE /patients/{id}, también soft), esta RPC queda
-- sin uso y se podrá dropear. `create or replace` la hace idempotente.

create or replace function public.admin_delete_patient(p_patient_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if not exists (
    select 1 from public.users
    where id = auth.uid() and role = 'super_admin' and active = true
  ) then
    raise exception 'Only an active super_admin may delete patients';
  end if;

  -- Baja lógica (no hard delete): marca el paciente como archivado. Sus consultas y events
  -- permanecen en la BD (trazabilidad); las listas de pacientes filtran deleted_at is null.
  update public.patients
    set deleted_at = now()
    where id = p_patient_id and deleted_at is null;
end;
$$;

grant execute on function public.admin_delete_patient(uuid) to authenticated;

