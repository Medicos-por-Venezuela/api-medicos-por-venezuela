-- Migración: users_specialty_id
-- Creada:    2026-08-13 14:43:05
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).

-- Cierra el último punto donde el matching dependía de una CADENA.
--
-- `users.specialty` guarda el NOMBRE de la especialidad, así que para saber si un médico atiende
-- salud mental había que buscar ese texto en el catálogo. Si la especialidad se renombraba, el
-- nombre guardado en `users` se quedaba viejo y dejaba de resolver. Con la FK, la regla cuelga de
-- la fila, y renombrar el catálogo no puede afectarla.
--
-- `users.specialty` NO se elimina: se mantiene como copia desnormalizada para mostrar y buscar
-- (`GET /profiles?search=`), y a partir de ahora se escribe SIEMPRE desde el catálogo. Aquí se
-- normaliza para que coincida exactamente con el nombre de la fila referenciada.
--
-- ON DELETE SET NULL igual que `doctors.specialty_id`: si una especialidad se borrara de verdad,
-- el médico queda sin ella (y fail-closed para salud mental) en vez de bloquear el borrado.

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS specialty_id uuid REFERENCES public.specialties(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_users_specialty_id ON public.users (specialty_id);

COMMENT ON COLUMN public.users.specialty_id IS
  'Especialidad del médico (FK al catálogo). Es la fuente de verdad para las reglas de la cola; '
  'users.specialty es solo la copia desnormalizada del nombre, para mostrar y buscar.';

DO $$
DECLARE
  v_por_nombre integer;
  v_por_alias  integer;
  v_sin_resolver integer;
BEGIN
  -- 1) Coincidencia exacta con el catálogo (2465 de 2960 filas en el backup de producción).
  UPDATE public.users u
  SET specialty_id = sp.id
  FROM public.specialties sp
  WHERE u.specialty_id IS NULL
    AND u.specialty IS NOT NULL
    AND u.specialty <> ''
    AND sp.deleted_at IS NULL
    AND lower(sp.name) = lower(u.specialty);
  GET DIAGNOSTICS v_por_nombre = ROW_COUNT;

  -- 2) Los nombres cortos de la lista vieja, que el catálogo renombró. Es la ÚLTIMA vez que este
  --    conocimiento hace falta: a partir de aquí manda la FK, no el texto.
  UPDATE public.users u
  SET specialty_id = sp.id
  FROM (VALUES
      ('pediatría',     'Pediatría y subespecialidades'),
      ('cirugía',       'Cirugía General y Digestivo'),
      ('traumatología', 'Traumatología y ortopedia'),
      ('fisiatría',     'Fisiatría y rehabilitacion')
  ) AS alias(viejo, nuevo)
  JOIN public.specialties sp ON lower(sp.name) = lower(alias.nuevo) AND sp.deleted_at IS NULL
  WHERE u.specialty_id IS NULL
    AND lower(u.specialty) = alias.viejo;
  GET DIAGNOSTICS v_por_alias = ROW_COUNT;

  -- 3) `users.specialty` pasa a ser copia exacta del nombre del catálogo, para que el texto y la
  --    FK no puedan contarse historias distintas.
  UPDATE public.users u
  SET specialty = sp.name
  FROM public.specialties sp
  WHERE u.specialty_id = sp.id
    AND u.specialty IS DISTINCT FROM sp.name;

  SELECT count(*) INTO v_sin_resolver
  FROM public.users
  WHERE specialty IS NOT NULL AND specialty <> '' AND specialty_id IS NULL;

  -- No se aborta: un médico sin especialidad resoluble es un problema de calidad de dato, no un
  -- bloqueo. Queda con specialty_id NULL, que es fail-closed para salud mental (sin la FK no
  -- puede tomar un caso psi). Se reporta para que se vea.
  IF v_sin_resolver > 0 THEN
    RAISE WARNING
      'users_specialty_id: % usuarios tienen un nombre de especialidad que no está en el '
      'catálogo; quedan con specialty_id NULL.', v_sin_resolver;
  END IF;

  RAISE NOTICE 'users_specialty_id: % por nombre, % por alias, % sin resolver.',
    v_por_nombre, v_por_alias, v_sin_resolver;
END $$;
