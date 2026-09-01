-- Migración: excluir otra del selector de interconsulta
-- Creada:    2026-08-31 22:07:11
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- "Otra" sale del selector de interconsultas, por el mismo motivo que 'Medicina general'
-- (ver 20260831_174358): una interconsulta busca a un ESPECIALISTA concreto, y "Otra" no
-- identifica a ninguno. Peor aún que Medicina general: difundir a "Otra" mandaría el caso a un
-- grupo heterogéneo donde nadie se siente aludido, así que probablemente no lo tomaría nadie.
--
-- Sigue disponible en el resto del sitio (registro de médicos, catálogos): esta columna solo
-- gobierna qué se puede PEDIR en una interconsulta.
--
-- Va por la columna y no por un filtro en el frontend por la razón de siempre en este repo: un
-- literal con el nombre se queda atrás en cuanto alguien renombra la fila del catálogo.

UPDATE public.specialties
   SET available_for_interconsultation = false,
       updated_at = now()
 WHERE lower(name) = 'otra'
   AND available_for_interconsultation;
