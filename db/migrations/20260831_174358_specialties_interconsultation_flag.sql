-- Migración: specialties interconsultation flag
-- Creada:    2026-08-31 17:43:58
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- Qué especialidades se pueden PEDIR en una interconsulta asíncrona.
--
-- La interconsulta existe para conseguir un ESPECIALISTA: un médico general pidiendo ayuda a
-- otro médico general no resuelve nada. Así que 'Medicina general' sale del selector.
--
-- La regla va en la BD y NO en un literal de Python, por la misma razón que los flags de salud
-- mental (ver 20260813_142814): este proyecto ya se quemó tres veces con nombres de especialidad
-- hardcodeados (el mapa SPECIALTY_NEEDS, la búsqueda de 'Pediatría' en el registro, y
-- _PSYCH_SPECIALTIES). Renombrar una fila del catálogo no puede cambiar una regla de negocio en
-- silencio. Con la columna, renombrar 'Medicina general' no la vuelve pedible de golpe.
--
-- Editable por admin (`catalogs.manage`): si mañana quieren excluir también 'Primeros auxilios',
-- o volver a incluir alguna, es un UPDATE — no un despliegue.

ALTER TABLE public.specialties
  ADD COLUMN IF NOT EXISTS available_for_interconsultation boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN public.specialties.available_for_interconsultation IS
  'Si esta especialidad se puede pedir en una interconsulta asíncrona. false para las que no '
  'son especialidades en el sentido del feature (Medicina general). Fuente de verdad de la '
  'regla: nunca un literal en el código.';

-- Default `true`: todo el catálogo queda pedible y solo se apagan las excepciones. Si mañana
-- entra una especialidad nueva, entra disponible — el fallo abierto es el correcto acá (perder
-- una especialidad del selector es peor que ofrecer una de más, que como mucho no tiene médicos).
UPDATE public.specialties
   SET available_for_interconsultation = false,
       updated_at = now()
 WHERE lower(name) = 'medicina general'
   AND available_for_interconsultation;
