-- Migración: specialties_flags_salud_mental
-- Creada:    2026-08-13 14:28:14
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).

-- Lleva la reserva de salud mental del código a la BD.
--
-- Hasta ahora la regla vivía en dos literales de Python:
--   _PSYCH_SPECIALTIES = {"Psicología", "Psiquiatría"}   y   specialty != "Psicología"
-- Es el mismo patrón que ya falló dos veces (el mapa SPECIALTY_NEEDS y la búsqueda de
-- 'Pediatría' en el registro): al renombrar una especialidad en el catálogo, el literal se queda
-- atrás. Aquí la consecuencia sería peor que perder un orden — si alguien renombra 'Psicología',
-- un caso de salud mental pasaría a poder tomarlo un médico general, en silencio.
--
-- Dos flags, porque son dos reglas distintas:
--   is_mental_health    -> la especialidad ATIENDE salud mental (Psicología y Psiquiatría).
--                          Un caso de salud mental solo lo puede tomar alguien con este flag.
--   mental_health_only  -> la especialidad SOLO atiende salud mental (Psicología). Un psicólogo
--                          no es médico, así que no puede tomar un caso de salud física; un
--                          psiquiatra sí. Por eso no basta un único booleano.
--
-- El CHECK impone que `mental_health_only` implique `is_mental_health`: "solo salud mental" sin
-- "atiende salud mental" sería un estado sin sentido que dejaría a esa especialidad sin poder
-- tomar nada.

ALTER TABLE public.specialties
  ADD COLUMN IF NOT EXISTS is_mental_health boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS mental_health_only boolean NOT NULL DEFAULT false;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_specialties_mental_health_coherente'
  ) THEN
    ALTER TABLE public.specialties
      ADD CONSTRAINT ck_specialties_mental_health_coherente
      CHECK (NOT mental_health_only OR is_mental_health);
  END IF;
END $$;

COMMENT ON COLUMN public.specialties.is_mental_health IS
  'La especialidad atiende salud mental. Un caso de salud mental solo lo puede tomar un médico '
  'con una especialidad marcada así (lo aplican get_panel y claim_consultation).';
COMMENT ON COLUMN public.specialties.mental_health_only IS
  'La especialidad SOLO atiende salud mental (p. ej. Psicología: no es médico). Implica '
  'is_mental_health.';

-- Semilla de la regla que hasta ahora estaba hardcodeada. Por nombre porque es la única señal
-- que hay hoy; a partir de aquí la fuente de verdad es la columna, no el nombre.
UPDATE public.specialties
SET is_mental_health = true
WHERE deleted_at IS NULL
  AND lower(name) IN ('psicología', 'psiquiatría')
  AND is_mental_health IS DISTINCT FROM true;

UPDATE public.specialties
SET mental_health_only = true
WHERE deleted_at IS NULL
  AND lower(name) = 'psicología'
  AND mental_health_only IS DISTINCT FROM true;

DO $$
DECLARE
  v_mh integer;
BEGIN
  SELECT count(*) INTO v_mh
  FROM public.specialties
  WHERE deleted_at IS NULL AND is_mental_health;

  -- Si el catálogo no tiene ninguna especialidad de salud mental, la reserva quedaría abierta:
  -- cualquier médico podría tomar un caso psi. Mejor abortar que aplicar una regla vacía.
  IF v_mh = 0 THEN
    RAISE EXCEPTION
      'specialties_flags_salud_mental: ninguna especialidad quedó marcada como salud mental. '
      'Revisa que el catálogo tenga Psicología / Psiquiatría antes de aplicar.';
  END IF;

  RAISE NOTICE 'specialties_flags_salud_mental: % especialidades de salud mental marcadas.', v_mh;
END $$;
