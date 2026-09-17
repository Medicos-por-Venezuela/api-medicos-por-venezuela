-- Migración: cola por especialidad y derivacion
-- Creada:    2026-09-17 11:40:58
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- Spec: tasks/cola-por-especialidad/spec.md. La cola deja de separar solo "salud mental vs.
-- física" y pasa a ser por especialidad exacta. Las reglas que no son "igualdad de ids" viven
-- aquí, en columnas y tablas sembradas UNA vez por nombre, y nunca en literales del código (la
-- misma convención de 20260813_142814 y 20260831_220711).

-- 1) "Otra" no identifica a ningún especialista ------------------------------------------------
-- Un médico con una especialidad de relleno no ve la cola hasta que diga cuál es la suya, y un
-- paciente no puede pedirla. Sigue en el catálogo: 528 médicos la tienen asignada.
ALTER TABLE public.specialties
  ADD COLUMN IF NOT EXISTS is_placeholder boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.specialties.is_placeholder IS
  'Especialidad de relleno ("Otra"): no identifica a ningún especialista. Un médico con ella no '
  've la cola hasta actualizar su perfil y un paciente no puede pedirla.';

UPDATE public.specialties
   SET is_placeholder = true,
       updated_at = now()
 WHERE lower(name) = 'otra'
   AND NOT is_placeholder;

-- 2) Colas que una especialidad ve además de la suya ------------------------------------------
CREATE TABLE IF NOT EXISTS public.specialty_queue_access (
  specialty_id       uuid NOT NULL REFERENCES public.specialties (id) ON DELETE CASCADE,
  extra_specialty_id uuid NOT NULL REFERENCES public.specialties (id) ON DELETE CASCADE,
  created_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (specialty_id, extra_specialty_id),
  CONSTRAINT specialty_queue_access_not_self CHECK (specialty_id <> extra_specialty_id)
);

COMMENT ON TABLE public.specialty_queue_access IS
  'Colas que los médicos de `specialty_id` ven y pueden tomar además de la suya. La cola es por '
  'especialidad exacta; esta tabla guarda las excepciones decididas por el equipo.';

-- Solo la API lee esta tabla.
ALTER TABLE public.specialty_queue_access ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON public.specialty_queue_access FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON public.specialty_queue_access FROM authenticated;
  END IF;
END $$;

-- Decisión del equipo (2026-09-17): por ahora Psiquiatría también atiende Psicología, y
-- Medicina interna sigue viendo Medicina general.
INSERT INTO public.specialty_queue_access (specialty_id, extra_specialty_id)
SELECT viewer.id, extra.id
  FROM (VALUES ('psiquiatría', 'psicología'),
               ('medicina interna', 'medicina general')) AS pair (viewer_name, extra_name)
  JOIN public.specialties viewer
    ON lower(viewer.name) = pair.viewer_name AND viewer.deleted_at IS NULL
  JOIN public.specialties extra
    ON lower(extra.name) = pair.extra_name AND extra.deleted_at IS NULL
ON CONFLICT DO NOTHING;

-- 3) De qué especialidad viene un caso derivado -----------------------------------------------
ALTER TABLE public.consultations
  ADD COLUMN IF NOT EXISTS derived_from_specialty_id uuid REFERENCES public.specialties (id);

COMMENT ON COLUMN public.consultations.derived_from_specialty_id IS
  'Especialidad desde la que se derivó este caso a la cola actual. NULL si nunca se derivó. El '
  'motivo y quién derivó están en el evento `derived`.';

-- La cola se ordena por hora de llegada del paciente (un derivado conserva la suya).
CREATE INDEX IF NOT EXISTS ix_consultations_queue
    ON public.consultations (specialty_id, queued_at)
 WHERE status = 'waiting' AND assigned_doctor_id IS NULL;

-- 4) Especialidad escrita a mano por el médico, pendiente de revisión --------------------------
ALTER TABLE public.doctors
  ADD COLUMN IF NOT EXISTS requested_specialty text,
  ADD COLUMN IF NOT EXISTS requested_specialty_at timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'doctors_requested_specialty_length'
  ) THEN
    ALTER TABLE public.doctors
      ADD CONSTRAINT doctors_requested_specialty_length
      CHECK (requested_specialty IS NULL OR char_length(btrim(requested_specialty)) BETWEEN 2 AND 120);
  END IF;
END $$;

COMMENT ON COLUMN public.doctors.requested_specialty IS
  'Especialidad que el médico escribió porque no está en el catálogo. Pendiente hasta que un '
  'admin la agregue o le asigne una existente; mientras tanto conserva su especialidad de relleno.';

CREATE INDEX IF NOT EXISTS ix_doctors_requested_specialty
    ON public.doctors (requested_specialty_at)
 WHERE requested_specialty IS NOT NULL AND deleted_at IS NULL;

-- 5) Datos: los casos en espera con "Otra" pasan a Medicina general ---------------------------
-- Desde aquí nadie ve la cola "Otra" (sus médicos quedan bloqueados y el registro deja de
-- ofrecerla). Medicina general es la puerta de entrada de triaje: desde ahí se deriva.
UPDATE public.consultations c
   SET specialty_id = general.id
  FROM public.specialties otra, public.specialties general
 WHERE c.specialty_id = otra.id
   AND otra.is_placeholder
   AND lower(general.name) = 'medicina general'
   AND general.deleted_at IS NULL
   AND c.status = 'waiting'
   AND c.assigned_doctor_id IS NULL;
