-- Migración: patients_emergency_contact_and_address
-- Creada:    2026-09-20 15:43:18
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).

alter table public.patients add column if not exists emergency_phone text;
alter table public.patients add column if not exists address_encrypted text;
comment on column public.patients.emergency_phone is
  'Teléfono de emergencia, distinto del WhatsApp. Se pide en el alta y lo ven admin y médico tratante.';
comment on column public.patients.address_encrypted is
  'Dirección cifrada E2E (v1:base64 sealed box). La API NUNCA la descifra, ni la loguea, ni la devuelve fuera de GET /patients/{id}/address.';