-- Migración: backfill doctors from users role doctor
-- Creada:    2026-07-04 10:39:02
--
-- Puebla public.doctors con los usuarios role='doctor' EXISTENTES de public.users (antes
-- 'profiles'). Los médicos NUEVOS los crea el frontend vía POST /api/v1/doctors (NO hay trigger).
--
-- Corre DESPUÉS del rename profiles->users (la FK doctors.user_id necesita la tabla users;
-- no se puede FK a la vista de compat). El origen de datos es el mismo.
--
-- Mapeos (según decisión del proyecto):
--   professional_type_id: specialty ILIKE 'psicolog%' -> 'Psicólogo', resto -> 'Médico'.
--   specialty_id:         match exacto por nombre; si falta en el catálogo, se crea.
--   phone:                se copia de users.whatsapp_number TAL CUAL (los legacy no cumplen el
--                         check +digits -> se quita el CHECK; el frontend obligará a corregirlo).
--   email:                se copia de users. cedula: null (se completará por el frontend).
--
-- Idempotente: guardas + inserts con NOT EXISTS + create or replace.

-- === 1) Catálogo: crear las specialties de médicos que aún no existan ===
insert into public.specialties (name)
select distinct trim(u.specialty)
from public.users u
where u.role = 'doctor'
  and nullif(trim(u.specialty), '') is not null
  and not exists (
    select 1 from public.specialties s
    where lower(s.name) = lower(trim(u.specialty)) and s.deleted_at is null
  );

-- === 2) Esquema de doctors: vínculo a la cuenta + relajar NOT NULL del dato legacy ===
alter table public.doctors add column if not exists user_id uuid;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'doctors_user_id_fkey' and conrelid = 'public.doctors'::regclass
  ) then
    alter table public.doctors
      add constraint doctors_user_id_fkey
      foreign key (user_id) references public.users (id) on delete set null;
  end if;
end $$;

-- 1:1 activo: un usuario no puede tener dos doctores vivos.
create unique index if not exists uq_doctors_user_id_not_deleted
  on public.doctors (user_id) where (deleted_at is null and user_id is not null);

alter table public.doctors alter column cedula drop not null;
alter table public.doctors alter column phone  drop not null;
alter table public.doctors alter column email  drop not null;

-- Guardamos el whatsapp legacy tal cual (no cumple ^\+\d{7,15}$); la validación de
-- formato pasa a la capa API (DoctorCreate/Update) y el frontend obliga a corregirlo.
alter table public.doctors drop constraint if exists doctors_phone_format;

-- === 3) Backfill: un doctor por cada usuario role='doctor' que aún no lo tenga ===
insert into public.doctors (
    id, user_id, full_name, email, professional_type_id, specialty_id,
    license, country_of_residence, cedula, phone, status, verified
)
select
    gen_random_uuid(),
    u.id,
    u.full_name,
    u.email,
    (select pt.id from public.professional_types pt
       where pt.name = case when u.specialty ilike 'psicolog%' then 'Psicólogo' else 'Médico' end
       limit 1),
    (select s.id from public.specialties s
       where lower(s.name) = lower(trim(u.specialty)) and s.deleted_at is null
       limit 1),
    u.medical_license,
    u.country,
    null,                          -- cedula: se completará por el frontend
    u.whatsapp_number,             -- phone: whatsapp legacy tal cual (a corregir por el frontend)
    1,                             -- status: activo
    coalesce(u.verified, false)
from public.users u
where u.role = 'doctor'
  and not exists (
    select 1 from public.doctors d where d.user_id = u.id and d.deleted_at is null
  );

-- === 4) Nuevos médicos: NO se auto-crean por trigger ===
-- El registro de médico del frontend ya crea el doctor vía POST /api/v1/doctors (con cédula,
-- teléfono y verificación SACS/FPV reales); ese endpoint liga la fila a la cuenta por email
-- (resuelve user_id en el servicio). Un trigger duplicaría la fila y chocaría con el unique de
-- email. Por eso aquí NO hay trigger; el backfill de arriba cubre solo a los médicos EXISTENTES.
-- (Se elimina un trigger previo por si esta migración ya se aplicó en una versión anterior.)
drop trigger if exists trg_sync_doctor_from_user on public.users;
drop function if exists public.sync_doctor_from_user();
