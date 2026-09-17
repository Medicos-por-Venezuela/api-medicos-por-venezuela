-- Migración: especialidades multiples por medico
-- Creada:    2026-09-17 16:23:16
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY.
--
-- Un médico puede ejercer VARIAS especialidades (un internista que además es cardiólogo) y quiere
-- ver las dos colas. `users.specialty_id` sigue existiendo como la **principal**: es la que usan el
-- pool, los reportes, el admin y la bandeja de interconsultas, y cambiar todo eso a la vez sería
-- un refactor mucho mayor que lo que pide el caso. Esta tabla es el CONJUNTO, y la cola lo lee.
--
-- Va por `user_id` y no por `doctors.id` porque la cola se decide con la cuenta (`users`), que es
-- lo que trae el JWT; la ficha sincroniza su principal a `users` desde hace tiempo.

CREATE TABLE IF NOT EXISTS public.doctor_specialties (
  user_id      uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
  specialty_id uuid NOT NULL REFERENCES public.specialties (id) ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, specialty_id)
);

COMMENT ON TABLE public.doctor_specialties IS
  'Especialidades que ejerce cada médico. `users.specialty_id` es la principal (pool, reportes, '
  'interconsultas); la cola del panel usa este conjunto.';

CREATE INDEX IF NOT EXISTS ix_doctor_specialties_specialty
    ON public.doctor_specialties (specialty_id);

-- Solo la API lee esta tabla.
ALTER TABLE public.doctor_specialties ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON public.doctor_specialties FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON public.doctor_specialties FROM authenticated;
  END IF;
END $$;

-- Backfill: la especialidad que cada cuenta tiene hoy es su primera (y por ahora única).
INSERT INTO public.doctor_specialties (user_id, specialty_id)
SELECT u.id, u.specialty_id
  FROM public.users u
 WHERE u.specialty_id IS NOT NULL
ON CONFLICT DO NOTHING;
