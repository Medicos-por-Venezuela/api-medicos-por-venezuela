-- Migración: backfill users from linked doctors
-- Creada:    2026-07-05 11:14:32
--
-- Los médicos que se registraron por el flujo nuevo (registro-medico + POST /doctors,
-- con verificación SACS/FPV) NUNCA propagaron specialty/country/medical_license/
-- whatsapp_number hacia su cuenta (users/profiles) — ese metadata no viaja en el
-- signUp de Supabase de ese flujo (a diferencia de /elegir-rol + set_my_role, que sí
-- los escribe directo). Por eso quedaban NULL en users pese a existir en doctors.
--
-- Este backfill sincroniza, una sola vez, todos los doctors ya ligados por user_id
-- (p. ej. fioreamm@gmail.com). De acá en adelante lo mantiene al día el propio backend
-- (create_doctor/update_doctor -> _sync_user_from_doctor). Idempotente: repetible sin
-- efectos distintos, solo re-escribe los mismos valores derivados de doctors.

update public.users u
set
    specialty = s.name,
    country = d.country_of_residence,
    medical_license = d.license,
    whatsapp_number = d.phone
from public.doctors d
left join public.specialties s on s.id = d.specialty_id
where d.user_id = u.id
  and d.deleted_at is null;
