-- Migración: add allergies parent_id parentesco to patients and specialty_id to consultations
-- Creada:    2026-07-05 17:40:32
--
-- Nuevo registro de pacientes: edad puntual (se sigue guardando en age_range, sin
-- cambio de columna), alergias como campo propio, y carga familiar (menor + adulto
-- responsable) vía auto-referencia en la misma tabla patients (no se justifica una
-- tabla aparte: el único dato distinto del menor es el parentesco).
--
-- needs_tags/description de patients quedan en su default (no se tocan acá): el
-- registro nuevo deja de completarlos, esa info pasa a vivir en la consulta
-- (chief_complaint + specialty_id, en vez de needs_tags). No se modifica el
-- matching/filtro de los paneles en esta migración.

alter table public.patients add column if not exists allergies text;
alter table public.patients add column if not exists parent_id uuid references public.patients (id) on delete set null;
alter table public.patients add column if not exists parentesco text;

create index if not exists idx_patients_parent_id on public.patients (parent_id);

alter table public.consultations add column if not exists specialty_id uuid references public.specialties (id);

create index if not exists idx_consultations_specialty_id on public.consultations (specialty_id);
