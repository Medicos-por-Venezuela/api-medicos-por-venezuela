-- Migración: merge_ginecologia_obstetricia
-- Creada:    2026-08-09 17:48:45
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).

-- Unifica las tres especialidades que en la práctica son la misma:
--   'Ginecología', 'Obstetricia' y 'Ginecología / obstetricia'  ->  'Ginecología y Obstetricia'
--
-- Se CONSERVA la fila de 'Ginecología / obstetricia' (solo se renombra), así que sus médicos no
-- necesitan repunte; a las otras dos se les repuntan las referencias y luego se dan de baja.
--
-- ⚠️ Baja LÓGICA (status='inactive' + deleted_at), no DELETE, por dos razones:
--   1) `doctors.specialty_id` es ON DELETE SET NULL: un DELETE dejaría médicos SIN especialidad
--      en silencio si algún repunte se escapara.
--   2) `consultations.specialty_id` es NO ACTION: un DELETE fallaría con cualquier consulta
--      histórica que aún apunte ahí.
--   Es además lo que hace el propio backend (services/specialties.py::delete_specialty).
--
-- ⚠️ La especialidad vive en DOS sitios y hay que tocar los dos:
--   - el catálogo, por id: `doctors.specialty_id` y `consultations.specialty_id`
--   - el TEXTO desnormalizado `users.specialty`, que es la columna con la que matchea la cola
--     (ver SPECIALTY_NEEDS en src/services/specialties.py y su espejo lib/utils.ts).
--
-- Idempotente: la segunda corrida no encuentra nada que mover y no hace nada.

DO $$
DECLARE
  v_destino   uuid;
  v_fusionar  uuid[];
  v_doctors   integer;
  v_consultas integer;
  v_users     integer;
BEGIN
  -- Fila superviviente: la ya renombrada (2ª corrida) o la original (1ª corrida).
  SELECT id INTO v_destino
  FROM public.specialties
  WHERE deleted_at IS NULL
    AND lower(name) IN ('ginecología y obstetricia', 'ginecología / obstetricia')
  ORDER BY (lower(name) = 'ginecología y obstetricia') DESC
  LIMIT 1;

  IF v_destino IS NULL THEN
    RAISE NOTICE 'merge_ginecologia_obstetricia: no existe la especialidad destino; nada que hacer.';
    RETURN;
  END IF;

  -- Las que se absorben (nunca la destino, por si los nombres cambiaran).
  SELECT coalesce(array_agg(id), '{}') INTO v_fusionar
  FROM public.specialties
  WHERE deleted_at IS NULL
    AND id <> v_destino
    AND lower(name) IN ('ginecología', 'obstetricia');

  -- 1) Renombrar la superviviente y colocarla donde estaban sus hermanas (iban en 4 y 5;
  --    'Ginecología / obstetricia' se había añadido al final con sort_order 1000).
  UPDATE public.specialties
  SET name = 'Ginecología y Obstetricia',
      sort_order = 4,
      updated_at = now()
  WHERE id = v_destino
    AND (name <> 'Ginecología y Obstetricia' OR sort_order <> 4);

  -- 2) Repuntar las referencias por id ANTES de dar de baja nada.
  UPDATE public.doctors
  SET specialty_id = v_destino
  WHERE specialty_id = ANY(v_fusionar);
  GET DIAGNOSTICS v_doctors = ROW_COUNT;

  UPDATE public.consultations
  SET specialty_id = v_destino
  WHERE specialty_id = ANY(v_fusionar);
  GET DIAGNOSTICS v_consultas = ROW_COUNT;

  -- 3) Repuntar el TEXTO de users.specialty (incluye el nombre viejo de la superviviente).
  UPDATE public.users
  SET specialty = 'Ginecología y Obstetricia'
  WHERE specialty IN ('Ginecología', 'Obstetricia', 'Ginecología / obstetricia');
  GET DIAGNOSTICS v_users = ROW_COUNT;

  -- 4) Baja lógica de las absorbidas, ya sin referencias vivas.
  UPDATE public.specialties
  SET status = 'inactive',
      deleted_at = now(),
      updated_at = now()
  WHERE id = ANY(v_fusionar)
    AND deleted_at IS NULL;

  RAISE NOTICE 'merge_ginecologia_obstetricia: doctors=% consultations=% users=% absorbidas=%',
    v_doctors, v_consultas, v_users, coalesce(array_length(v_fusionar, 1), 0);
END $$;
