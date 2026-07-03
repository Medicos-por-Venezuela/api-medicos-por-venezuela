CREATE TABLE IF NOT EXISTS public.affected_zones (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    state text NOT NULL,
    country text DEFAULT 'Venezuela',
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'deleted')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_affected_zones_name_state_not_deleted
    ON public.affected_zones (lower(name), lower(state)) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_affected_zones_status ON public.affected_zones (status);
CREATE INDEX IF NOT EXISTS idx_affected_zones_state ON public.affected_zones (state);
CREATE INDEX IF NOT EXISTS idx_affected_zones_deleted_at ON public.affected_zones (deleted_at);

ALTER TABLE public.affected_zones ENABLE ROW LEVEL SECURITY;
