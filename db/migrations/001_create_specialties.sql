create table if not exists public.specialties (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    status text not null default 'active' check (status in ('active', 'inactive')),
    sort_order integer not null default 1000,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz
);

alter table public.specialties add column if not exists status text not null default 'active';
alter table public.specialties add column if not exists sort_order integer not null default 1000;
alter table public.specialties add column if not exists created_at timestamptz not null default now();
alter table public.specialties add column if not exists updated_at timestamptz not null default now();
alter table public.specialties add column if not exists deleted_at timestamptz;

with duplicate_specialties as (
    select id,
           row_number() over (
               partition by lower(name)
               order by (status = 'active') desc, sort_order asc, created_at asc, id asc
           ) as row_number
    from public.specialties
    where deleted_at is null
)
update public.specialties specialties
set status = 'inactive', deleted_at = coalesce(specialties.deleted_at, now())
from duplicate_specialties duplicates
where specialties.id = duplicates.id and duplicates.row_number > 1;

create unique index if not exists uq_specialties_name_not_deleted
    on public.specialties (lower(name)) where deleted_at is null;
create index if not exists idx_specialties_deleted_at on public.specialties (deleted_at);
create index if not exists idx_specialties_status on public.specialties (status);
create index if not exists idx_specialties_sort_order on public.specialties (sort_order);

with rows(name, sort_order) as (
    values
        ('Medicina general', 1),
        ('Pediatría', 2),
        ('Traumatología', 3),
        ('Ginecología', 4),
        ('Obstetricia', 5),
        ('Cardiología', 6),
        ('Medicina interna', 7),
        ('Psicología', 8),
        ('Psiquiatría', 9),
        ('Neurología', 10),
        ('Cirugía', 11),
        ('Oncología', 12),
        ('Oncología médica', 13),
        ('Fisiatría', 14),
        ('Cuidados paliativos y manejo del dolor', 15),
        ('Geriatría', 16),
        ('Reumatología', 17),
        ('Otra', 18)
)
insert into public.specialties (name, status, sort_order)
select rows.name, 'active', rows.sort_order
from rows
where not exists (
    select 1
    from public.specialties specialties
    where lower(specialties.name) = lower(rows.name)
      and specialties.deleted_at is null
);

alter table public.specialties enable row level security;
