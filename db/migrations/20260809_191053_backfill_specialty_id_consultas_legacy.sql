-- Migración: backfill_specialty_id_consultas_legacy
-- Creada:    2026-08-09 19:10:53
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).

-- Rellena `consultations.specialty_id` en las consultas anteriores a esa columna, para que el
-- matching sea SIEMPRE por la columna y no queden filas sin especialidad.
--
-- Es el cierre del mapa legacy `SPECIALTY_NEEDS` (necesidad del paciente -> especialidad que la
-- cubre): ese diccionario vivía hardcodeado en el backend Y copiado en el frontend, y se
-- desincronizaba del catálogo cada vez que se renombraba una especialidad. Aquí se usa su
-- conocimiento UNA ÚLTIMA VEZ para pasarlo a datos, y el código se elimina.
--
-- Señal: `consultations.category` (la "necesidad" que eligió el paciente) y, si viene vacía, el
-- primer `patients.needs_tags`. Medido antes de escribirla: 215 de 216 filas tienen category, y
-- ninguna se queda sin señal.
--
-- ⚠️ NO cambia ninguna decisión de cola ni de claim. Las 216 filas están cerradas o ya asignadas:
-- ninguna está en `waiting`, y las 3 sin asignar son `closed`/`closed_by_admin`, que no entran en
-- `_PANEL_WAITING_STATUSES`. Además el mapeo preserva la reserva de salud mental: los casos de
-- 'Apoyo emocional' y 'Crisis de ansiedad' van a Psicología, y con specialty_id puesto
-- `can_attend_consultation` exige médico psi igual que hacía `RESERVED_NEEDS` sin él.
--
-- Requiere la migración de fusión de Ginecología/Obstetricia (va antes por nombre): 'Embarazo'
-- apunta a 'Ginecología y Obstetricia'.
--
-- Idempotente: solo toca filas con specialty_id IS NULL; la segunda corrida no encuentra ninguna.

DO $$
DECLARE
  v_pendientes_antes  integer;
  v_actualizadas      integer;
  v_pendientes_despues integer;
BEGIN
  SELECT count(*) INTO v_pendientes_antes
  FROM public.consultations WHERE specialty_id IS NULL;

  IF v_pendientes_antes = 0 THEN
    RAISE NOTICE 'backfill_specialty_id: no hay consultas sin specialty_id; nada que hacer.';
    RETURN;
  END IF;

  WITH mapa(necesidad, especialidad) AS (
    VALUES
      -- Salud mental: van a Psicología para conservar la reserva (ver nota de arriba).
      ('Apoyo emocional',    'Psicología'),
      ('Crisis de ansiedad', 'Psicología'),
      -- Atención general: la necesidad no acota a una especialidad concreta.
      ('Medicina general',   'Medicina general'),
      ('Primeros auxilios',  'Medicina general'),
      ('Medicamentos',       'Medicina general'),
      -- Las que el mapa legacy sí acotaba, con el nombre ACTUAL del catálogo.
      ('Enfermedad crónica', 'Medicina interna'),
      ('Lesión física',      'Traumatología y ortopedia'),
      ('Niño / pediatría',   'Pediatría y subespecialidades'),
      ('Embarazo',           'Ginecología y Obstetricia'),
      -- Sin información suficiente: 'Otra' antes que inventar una especialidad.
      ('Otra',               'Otra')
  ),
  pendientes AS (
    SELECT c.id AS consultation_id,
           COALESCE(NULLIF(c.category, ''), p.needs_tags[1]) AS necesidad
    FROM public.consultations c
    LEFT JOIN public.patients p ON p.id = c.patient_id
    WHERE c.specialty_id IS NULL
  ),
  resueltas AS (
    -- Toda necesidad desconocida (o ausente) cae en 'Otra': el objetivo es que no quede NULL.
    SELECT pe.consultation_id, COALESCE(m.especialidad, 'Otra') AS especialidad
    FROM pendientes pe
    LEFT JOIN mapa m ON m.necesidad = pe.necesidad
  )
  UPDATE public.consultations c
  SET specialty_id = sp.id
  FROM resueltas r
  JOIN public.specialties sp
    ON lower(sp.name) = lower(r.especialidad)
   AND sp.deleted_at IS NULL
  WHERE c.id = r.consultation_id;

  GET DIAGNOSTICS v_actualizadas = ROW_COUNT;

  SELECT count(*) INTO v_pendientes_despues
  FROM public.consultations WHERE specialty_id IS NULL;

  -- El objetivo es explícito: no dejar data vacía. Si alguna fila se quedó sin especialidad
  -- (p. ej. porque falta una especialidad del catálogo), se aborta entera en vez de dejar el
  -- backfill a medias y que nadie se entere.
  IF v_pendientes_despues > 0 THEN
    RAISE EXCEPTION
      'backfill_specialty_id: quedaron % consultas sin specialty_id (de % iniciales). '
      'Revisa que existan en el catálogo las especialidades destino del mapeo.',
      v_pendientes_despues, v_pendientes_antes;
  END IF;

  RAISE NOTICE 'backfill_specialty_id: % consultas actualizadas, 0 sin especialidad.',
    v_actualizadas;
END $$;
