-- Migración: revoke residual patient role from real doctors
-- Creada:    2026-07-13 15:22:35
--
-- Contexto: el trigger `sync_user_roles_from_profile` (20260703_221135) es ADITIVO:
-- cuando `profiles.role` cambia, agrega el nuevo rol a `user_roles` pero NO revoca el
-- anterior. El backfill de doctores (20260704_103902) volteó a varios usuarios de
-- 'patient' a 'doctor', dejándoles pegado un rol 'patient' que ya no corresponde.
--
-- Esta migración revoca ese 'patient' RESIDUAL — y solo el residual — usando revocación
-- SUAVE (`revoked_at = now()`), idéntica a la que hace la API (`revoke_role`) y al índice
-- único parcial `uq_user_roles_active (... WHERE revoked_at IS NULL)`. No borra filas: deja
-- historial y es reversible.
--
-- Guardas (deben cumplirse TODAS para tocar la fila; si no, se respeta el rol):
--   1. es un rol 'patient' actualmente activo,
--   2. el usuario también tiene el rol 'doctor' activo (era un dual doctor+patient),
--   3. tiene ficha en `doctors` (es un médico real), y
--   4. NO tiene registro en `patients` (nunca fue paciente real; esto implica además que
--      no puede figurar como `patient_id` en ninguna consulta).
-- Un usuario que sea médico Y paciente real (con fila en `patients`) conserva ambos roles.
--
-- Idempotente: en corridas posteriores no quedan filas 'patient' activas que cumplan las
-- guardas, así que actualiza 0.

update public.user_roles ur
set revoked_at = now()
from public.roles r
where ur.role_id = r.id
  and r.code = 'patient'
  and ur.revoked_at is null
  -- (2) tiene también el rol 'doctor' activo
  and exists (
      select 1
      from public.user_roles urd
      join public.roles rd on rd.id = urd.role_id
      where urd.user_id = ur.user_id
        and rd.code = 'doctor'
        and rd.deleted_at is null
        and urd.revoked_at is null
  )
  -- (3) es un médico real: tiene ficha en doctors
  and exists (select 1 from public.doctors d where d.user_id = ur.user_id)
  -- (4) no es un paciente real: sin registro en patients
  and not exists (select 1 from public.patients pt where pt.user_id = ur.user_id);
