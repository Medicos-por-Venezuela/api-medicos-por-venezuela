-- Migración: cola de triaje para especialistas
-- Creada:    2026-09-17 16:05:45
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY.
--
-- La cola de Medicina general es la PUERTA DE ENTRADA: ahí caen los pacientes que no saben qué
-- especialidad necesitan, y es la que más acumula. Los especialistas de salud física la ven
-- también, en una cola aparte de la suya (el panel les muestra las dos por separado).
--
-- Va por columna y no por nombre, como el resto de reglas del catálogo (ver 20260917_114058):
-- si mañana la puerta de entrada se llama distinto, se marca la fila y ya.

ALTER TABLE public.specialties
  ADD COLUMN IF NOT EXISTS is_general_triage boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.specialties.is_general_triage IS
  'Cola de entrada (Medicina general): además de sus médicos, la ven todos los que atienden '
  'salud física. Quien solo atiende salud mental (mental_health_only) NO la ve.';

UPDATE public.specialties
   SET is_general_triage = true,
       updated_at = now()
 WHERE lower(name) = 'medicina general'
   AND NOT is_general_triage;
