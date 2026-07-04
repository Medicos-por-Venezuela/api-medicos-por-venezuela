-- Migración: RBAC multi-rol granular + audit log
-- Modelo: profiles ─<user_roles>─ roles ─<role_permissions>─ permissions
-- Un usuario puede tener varios roles; sus permisos = unión de los de sus roles.
-- Idempotente. Todas las tablas con RLS deny-all (solo la API como owner accede).

-- === Roles ===
create table if not exists public.roles (
    id          uuid primary key default gen_random_uuid(),
    code        text not null,                 -- clave estable: 'admin', 'doctor', ...
    name        text not null,                 -- nombre visible
    description text,
    is_system   boolean not null default false, -- los del sistema no se borran
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    deleted_at  timestamptz
);
create unique index if not exists uq_roles_code_not_deleted
    on public.roles (code) where deleted_at is null;

-- === Permisos ===
create table if not exists public.permissions (
    id          uuid primary key default gen_random_uuid(),
    code        text not null,                 -- 'consultations.read', 'doctors.verify', ...
    description text,
    created_at  timestamptz not null default now()
);
create unique index if not exists uq_permissions_code on public.permissions (code);

-- === Rol ↔ Permiso (N:M) ===
create table if not exists public.role_permissions (
    role_id       uuid not null references public.roles (id) on delete cascade,
    permission_id uuid not null references public.permissions (id) on delete cascade,
    primary key (role_id, permission_id)
);
create index if not exists idx_role_permissions_permission
    on public.role_permissions (permission_id);

-- === Usuario ↔ Rol (N:M) — el corazón del multi-rol ===
create table if not exists public.user_roles (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references public.profiles (id) on delete cascade,
    role_id     uuid not null references public.roles (id) on delete cascade,
    assigned_by uuid references public.profiles (id) on delete set null, -- null = sistema/trigger
    assigned_at timestamptz not null default now(),
    revoked_at  timestamptz                                              -- soft-revoke: conserva historial
);
-- Un usuario no puede tener el mismo rol activo dos veces.
create unique index if not exists uq_user_roles_active
    on public.user_roles (user_id, role_id) where revoked_at is null;
create index if not exists idx_user_roles_user on public.user_roles (user_id);
create index if not exists idx_user_roles_role on public.user_roles (role_id);

-- === Audit log (append-only, inmutable) ===
create table if not exists public.audit_log (
    id             uuid primary key default gen_random_uuid(),
    actor_user_id  uuid references public.profiles (id) on delete set null, -- quién; null = sistema
    action         text not null,        -- 'role.assigned', 'doctor.verified', 'consultation.closed'...
    resource       text,                 -- 'user_roles', 'doctors', ...
    resource_id    text,                 -- id del recurso afectado (text: admite no-uuid)
    metadata       jsonb,                -- contexto extra (valores viejos/nuevos, etc.)
    ip             text,
    correlation_id text,                 -- ata con el X-Correlation-ID del request
    created_at     timestamptz not null default now()
);
create index if not exists idx_audit_log_actor on public.audit_log (actor_user_id);
create index if not exists idx_audit_log_action on public.audit_log (action);
create index if not exists idx_audit_log_resource on public.audit_log (resource, resource_id);
create index if not exists idx_audit_log_created on public.audit_log (created_at desc);

-- Inmutabilidad (no-repudio): el audit_log no se puede modificar ni borrar.
create or replace function public.audit_log_block_write() returns trigger
language plpgsql as $$
begin
    raise exception 'audit_log es inmutable: no se permite % ', tg_op;
end;
$$;
drop trigger if exists trg_audit_log_immutable on public.audit_log;
create trigger trg_audit_log_immutable
    before update or delete on public.audit_log
    for each row execute function public.audit_log_block_write();

-- === RLS deny-all en todas (solo la API/owner accede) ===
alter table public.roles            enable row level security;
alter table public.permissions      enable row level security;
alter table public.role_permissions enable row level security;
alter table public.user_roles       enable row level security;
alter table public.audit_log        enable row level security;
