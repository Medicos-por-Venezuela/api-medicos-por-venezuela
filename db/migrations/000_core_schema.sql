-- Migración: core schema (Auth + datos base) — sincronizada 1:1 con
-- supabase_schema.sql del repo del frontend (medicos-por-venezuela).
--
-- Ordena PRIMERA (000_) porque el resto de las migraciones de este repo (catálogos,
-- doctors, RBAC, rename profiles->users) dependen de `public.profiles`/`auth.users`.
-- Se aplica contra el Postgres del Supabase LOCAL (`supabase start`), que ya trae el
-- esquema `auth` (Auth/gotrue) creado — por eso ya NO hace falta el stub de
-- `db/init/00-supabase-stubs.sql` para levantar el entorno local.
--
-- Fuente de verdad del schema "core" (profiles/patients/consultations/RLS/RPCs):
-- el frontend. Si cambia supabase_schema.sql allá, replicar el diff acá.
--
-- Ya era idempotente en origen (if not exists / or replace); se copia verbatim.

create extension if not exists pgcrypto;

-- 1) Staff profiles linked to Supabase Auth users
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text unique,
  full_name text not null,
  role text not null default 'doctor', -- doctor | specialist | admin | super_admin
  specialty text,
  medical_license text,
  country text,
  whatsapp_number text,
  verified boolean not null default false,
  active boolean not null default true,
  role_chosen boolean not null default false, -- false = OAuth placeholder waiting for the user to pick a role
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  constraint profiles_role_check check (role in ('patient', 'doctor', 'specialist', 'admin', 'super_admin'))
);

alter table public.profiles add column if not exists email text unique;
alter table public.profiles add column if not exists full_name text;
alter table public.profiles add column if not exists role text default 'doctor';
alter table public.profiles add column if not exists specialty text;
alter table public.profiles add column if not exists medical_license text;
alter table public.profiles add column if not exists country text;
alter table public.profiles add column if not exists whatsapp_number text;
alter table public.profiles add column if not exists verified boolean default false;
alter table public.profiles add column if not exists active boolean default true;
alter table public.profiles add column if not exists role_chosen boolean not null default false;
-- Backfill: accounts that predate the role picker already have a real role (only fresh OAuth
-- placeholders use role='patient' with role_chosen=false), so finalize the existing staff rows.
update public.profiles set role_chosen = true where role <> 'patient' and role_chosen = false;
alter table public.profiles add column if not exists last_seen_at timestamptz;
alter table public.profiles add column if not exists created_at timestamptz default now();

-- Allow 'patient' in the role constraint (idempotent for existing databases).
alter table public.profiles drop constraint if exists profiles_role_check;
alter table public.profiles add constraint profiles_role_check
  check (role in ('patient', 'doctor', 'specialist', 'admin', 'super_admin'));

-- 2) Doctor volunteer applications: RETIRED. Doctors now self-register as real accounts.
-- Dropping the table also removes its policies (cascade); safe whether or not it exists.
drop table if exists public.doctor_applications cascade;

-- 3) Patients. Keep this minimal; avoid storing more health data than needed.
-- user_id links a patient record to an (optional) Supabase Auth account so they can follow their case.
create table if not exists public.patients (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete set null,
  full_name text not null,
  cedula text,
  phone_whatsapp text not null,
  affected_zone text not null,
  age_range text,
  needs_tags text[] not null default '{}',
  description text,
  consent boolean not null default false,
  consent_at timestamptz,
  created_at timestamptz not null default now()
);

alter table public.patients add column if not exists user_id uuid references auth.users(id) on delete set null;
alter table public.patients add column if not exists cedula text;
alter table public.patients add column if not exists age_range text;
alter table public.patients add column if not exists needs_tags text[] default '{}';
alter table public.patients add column if not exists description text;
alter table public.patients add column if not exists consent boolean default false;
alter table public.patients add column if not exists consent_at timestamptz;
alter table public.patients add column if not exists created_at timestamptz default now();
-- Optional contact email — a fallback when the phone number doesn't work.
alter table public.patients add column if not exists email text;

-- 4) Consultations/cases. WhatsApp messages are not stored here.
create table if not exists public.consultations (
  id uuid primary key default gen_random_uuid(),
  patient_id uuid not null references public.patients(id) on delete cascade,
  code text unique not null,
  status text not null default 'waiting', -- waiting | in_progress | referred_to_specialist | urgent_in_person | closed | cancelled
  priority text not null default 'normal',
  category text,
  chief_complaint text,
  assigned_doctor_id uuid references public.profiles(id),
  referred_specialty text,
  internal_note text,
  video_room_url text,
  patient_last_seen_at timestamptz, -- heartbeat: last time the patient's waiting-room page pinged
  entered_call_at timestamptz, -- first time the patient clicked "Entrar a la videoconsulta"
  opened_at timestamptz,
  closed_at timestamptz,
  created_at timestamptz not null default now(),
  constraint consultations_status_check check (status in ('waiting', 'in_progress', 'referred_to_specialist', 'urgent_in_person', 'closed', 'cancelled', 'patient_no_show', 'closed_by_admin', 'contacted_whatsapp'))
);

alter table public.consultations add column if not exists category text;
alter table public.consultations add column if not exists chief_complaint text;
alter table public.consultations add column if not exists assigned_doctor_id uuid references public.profiles(id);
alter table public.consultations add column if not exists referred_specialty text;
alter table public.consultations add column if not exists internal_note text;
alter table public.consultations add column if not exists video_room_url text;
alter table public.consultations add column if not exists patient_last_seen_at timestamptz;
alter table public.consultations add column if not exists entered_call_at timestamptz;
alter table public.consultations add column if not exists opened_at timestamptz;
alter table public.consultations add column if not exists closed_at timestamptz;
alter table public.consultations add column if not exists created_at timestamptz default now();
-- Admin follow-up flag: whether an admin has contacted the patient about this case (set from the
-- admin dashboard, independent of status).
alter table public.consultations add column if not exists contacted boolean not null default false;
-- Admin case follow-up: which super_admin is following up (admin_seguimiento) and a free-text admin
-- note (nota_admin). Both are edited from the admin dashboard cases table.
alter table public.consultations add column if not exists admin_seguimiento uuid references public.profiles(id);
alter table public.consultations add column if not exists nota_admin text;
-- Doctor chose to attend this case directly via WhatsApp (their personal number) instead of video;
-- the case-detail page then shows the WhatsApp status controls and hides the video/close buttons.
alter table public.consultations add column if not exists attended_via_whatsapp boolean not null default false;

-- Allow the 'patient_no_show' and 'closed_by_admin' statuses on existing databases (idempotent).
alter table public.consultations drop constraint if exists consultations_status_check;
alter table public.consultations add constraint consultations_status_check
  check (status in ('waiting', 'in_progress', 'referred_to_specialist', 'urgent_in_person', 'closed', 'cancelled', 'patient_no_show', 'closed_by_admin', 'contacted_whatsapp'));

-- 5) Events/audit trail for status changes.
create table if not exists public.consultation_events (
  id uuid primary key default gen_random_uuid(),
  consultation_id uuid not null references public.consultations(id) on delete cascade,
  event_type text not null,
  created_by uuid references public.profiles(id) default auth.uid(),
  note text,
  created_at timestamptz not null default now()
);

-- Ensure cascading deletes on EXISTING databases. `create table if not exists` never alters a
-- foreign key that was first created without ON DELETE CASCADE, so older databases keep the default
-- NO ACTION and block deleting a patient (you'd get a foreign-key violation from `consultations`).
-- Re-applying the constraints idempotently makes "delete a patient" also remove their consultations
-- and the related audit events. (Constraint names are Postgres' defaults: <table>_<column>_fkey.)
alter table public.consultations drop constraint if exists consultations_patient_id_fkey;
alter table public.consultations
  add constraint consultations_patient_id_fkey
  foreign key (patient_id) references public.patients(id) on delete cascade;

alter table public.consultation_events drop constraint if exists consultation_events_consultation_id_fkey;
alter table public.consultation_events
  add constraint consultation_events_consultation_id_fkey
  foreign key (consultation_id) references public.consultations(id) on delete cascade;

create index if not exists idx_profiles_role on public.profiles(role);
create index if not exists idx_profiles_last_seen on public.profiles(last_seen_at);
create index if not exists idx_consultations_status on public.consultations(status);
create index if not exists idx_consultations_assigned on public.consultations(assigned_doctor_id);
create index if not exists idx_consultations_patient on public.consultations(patient_id);
create index if not exists idx_patients_user on public.patients(user_id);
create index if not exists idx_consultation_events_consultation on public.consultation_events(consultation_id);

-- Create a profile automatically when a Supabase Auth user is created.
-- Email signups pass role + (for doctors) professional fields via user metadata and are finalized
-- immediately (role_chosen = true, instant access). OAuth signups (Google) have no role metadata,
-- so they get a placeholder profile with role_chosen = false; the app then sends them to /elegir-rol.
-- Never assigns admin/specialist here; those are promoted manually.
create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  meta_role text := new.raw_user_meta_data->>'role';
  resolved_role text;
  has_role boolean;
begin
  -- coalesce: NULL metadata (e.g. Google sign-in) must yield false, not NULL,
  -- because role_chosen is NOT NULL.
  has_role := coalesce(meta_role in ('patient', 'doctor'), false);
  resolved_role := case when has_role then meta_role else 'patient' end;

  insert into public.profiles (
    id, email, full_name, role,
    specialty, country, medical_license, whatsapp_number,
    verified, active, role_chosen
  )
  values (
    new.id,
    new.email,
    coalesce(
      new.raw_user_meta_data->>'full_name',
      new.raw_user_meta_data->>'name',
      split_part(new.email, '@', 1)
    ),
    resolved_role,
    case when resolved_role = 'doctor' then new.raw_user_meta_data->>'specialty' end,
    case when resolved_role = 'doctor' then new.raw_user_meta_data->>'country' end,
    case when resolved_role = 'doctor' then new.raw_user_meta_data->>'medical_license' end,
    case when resolved_role = 'doctor' then new.raw_user_meta_data->>'whatsapp_number' end,
    true,
    true,
    has_role -- email signup with an explicit role is finalized; OAuth placeholders are not
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

-- Users finalize their OWN profile exactly once (used by /elegir-rol after Google sign-in).
-- Cannot be used to escalate: only patient/doctor, only while role_chosen is still false.
create or replace function public.set_my_role(
  p_role text,
  p_specialty text default null,
  p_country text default null,
  p_medical_license text default null,
  p_whatsapp_number text default null
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_role not in ('patient', 'doctor') then
    raise exception 'invalid role';
  end if;

  update public.profiles
  set
    role = p_role,
    specialty = case when p_role = 'doctor' then p_specialty else specialty end,
    country = case when p_role = 'doctor' then p_country else country end,
    medical_license = case when p_role = 'doctor' then p_medical_license else medical_license end,
    whatsapp_number = case when p_role = 'doctor' then p_whatsapp_number else whatsapp_number end,
    verified = true,
    active = true,
    role_chosen = true
  where id = auth.uid() and role_chosen = false;
end;
$$;

grant execute on function public.set_my_role(text, text, text, text, text) to authenticated;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
after insert on auth.users
for each row execute procedure public.handle_new_auth_user();

-- Helper functions for RLS
create or replace function public.current_user_role()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select role from public.profiles where id = auth.uid() and active = true and verified = true;
$$;

create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(public.current_user_role() in ('admin', 'super_admin'), false);
$$;

create or replace function public.is_staff()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(public.current_user_role() in ('doctor', 'specialist', 'admin', 'super_admin'), false);
$$;

-- Doctors call this RPC from the browser; they cannot update their profile role/permissions directly.
create or replace function public.mark_myself_online()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.profiles set last_seen_at = now() where id = auth.uid();
end;
$$;

grant execute on function public.mark_myself_online() to authenticated;

-- Patient waiting-room heartbeat. The patient's /sala-espera page calls this every ~20s while open
-- so the doctor panel can tell who is actually present (vs. who submitted and left). Security definer
-- so anonymous patients (no auth) can update only this one timestamp, and only on still-open cases.
create or replace function public.mark_patient_waiting(p_consultation_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.consultations
  set patient_last_seen_at = now()
  where id = p_consultation_id
    and status in ('waiting', 'in_progress');
end;
$$;

grant execute on function public.mark_patient_waiting(uuid) to anon, authenticated;

-- Patient clicked "Entrar a la videoconsulta". Records the first click (entered_call_at is only set
-- once, via coalesce) and refreshes the presence heartbeat. Used so admin metrics count a case as
-- "esperando" only after the patient has actually entered the call — not the moment they submitted.
-- Security definer so anonymous patients can set only this timestamp, and only on still-open cases.
create or replace function public.mark_patient_entered_call(p_consultation_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.consultations
  set entered_call_at = coalesce(entered_call_at, now()),
      patient_last_seen_at = now()
  where id = p_consultation_id
    and status in ('waiting', 'in_progress');
end;
$$;

grant execute on function public.mark_patient_entered_call(uuid) to anon, authenticated;

-- Hard-delete a patient and everything tied to them (consultations + their audit events). Reserved
-- for super_admins (used by the admin dashboard to remove test/junk records). Security definer so it
-- can delete across tables regardless of RLS; it deletes children explicitly, so it does not rely on
-- the FK ON DELETE CASCADE being applied. Irreversible.
create or replace function public.admin_delete_patient(p_patient_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if not exists (
    select 1 from public.profiles
    where id = auth.uid() and role = 'super_admin' and active = true
  ) then
    raise exception 'Only an active super_admin may delete patients';
  end if;

  delete from public.consultation_events
    where consultation_id in (select id from public.consultations where patient_id = p_patient_id);
  delete from public.consultations where patient_id = p_patient_id;
  delete from public.patients where id = p_patient_id;
end;
$$;

grant execute on function public.admin_delete_patient(uuid) to authenticated;

-- Enable Row Level Security
alter table public.profiles enable row level security;
alter table public.patients enable row level security;
alter table public.consultations enable row level security;
alter table public.consultation_events enable row level security;

-- Drop old policies safely
-- profiles
drop policy if exists profiles_select_self_or_admin on public.profiles;
drop policy if exists profiles_insert_admin on public.profiles;
drop policy if exists profiles_update_admin on public.profiles;
-- patients
drop policy if exists patients_insert_public on public.patients;
drop policy if exists patients_select_staff on public.patients;
drop policy if exists patients_select_own on public.patients;
drop policy if exists patients_update_admin on public.patients;
-- consultations
drop policy if exists consultations_insert_public on public.consultations;
drop policy if exists consultations_select_staff on public.consultations;
drop policy if exists consultations_select_own on public.consultations;
drop policy if exists consultations_update_staff on public.consultations;
drop policy if exists consultations_update_admin on public.consultations;
-- events
drop policy if exists events_select_staff on public.consultation_events;
drop policy if exists events_insert_staff on public.consultation_events;

-- profiles policies
create policy profiles_select_self_or_admin
on public.profiles
for select
to authenticated
using (auth.uid() = id or public.is_admin());

create policy profiles_insert_admin
on public.profiles
for insert
to authenticated
with check (public.is_admin());

create policy profiles_update_admin
on public.profiles
for update
to authenticated
using (public.is_admin())
with check (public.is_admin());

-- patients policies
create policy patients_insert_public
on public.patients
for insert
to anon, authenticated
with check (consent = true and (user_id is null or user_id = auth.uid()));

create policy patients_select_staff
on public.patients
for select
to authenticated
using (public.is_staff());

-- A patient with an account can read only their own record.
create policy patients_select_own
on public.patients
for select
to authenticated
using (user_id = auth.uid());

create policy patients_update_admin
on public.patients
for update
to authenticated
using (public.is_admin())
with check (public.is_admin());

-- consultations policies
create policy consultations_insert_public
on public.consultations
for insert
to anon, authenticated
with check (status = 'waiting' and assigned_doctor_id is null);

create policy consultations_select_staff
on public.consultations
for select
to authenticated
using (public.is_staff());

-- A patient with an account can read only the consultations tied to their own patient record.
create policy consultations_select_own
on public.consultations
for select
to authenticated
using (
  exists (
    select 1 from public.patients p
    where p.id = consultations.patient_id and p.user_id = auth.uid()
  )
);

create policy consultations_update_staff
on public.consultations
for update
to authenticated
using (
  public.current_user_role() in ('doctor', 'specialist')
  and (assigned_doctor_id is null or assigned_doctor_id = auth.uid())
)
with check (
  public.current_user_role() in ('doctor', 'specialist')
  and (assigned_doctor_id = auth.uid() or assigned_doctor_id is null)
);

create policy consultations_update_admin
on public.consultations
for update
to authenticated
using (public.is_admin())
with check (public.is_admin());

-- consultation events policies
create policy events_select_staff
on public.consultation_events
for select
to authenticated
using (public.is_staff());

create policy events_insert_staff
on public.consultation_events
for insert
to authenticated
with check (public.is_staff());
