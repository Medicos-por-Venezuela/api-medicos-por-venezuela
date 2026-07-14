-- Migración: seed professional types (Médico, Psicólogo)
-- Tipos base para el ruteo de verificación de credenciales al registrar un médico:
-- Médico -> SACS · Psicólogo -> FPV. Idempotente.

insert into public.professional_types (name, status)
select v.name, 'active'
from (values ('Médico'), ('Psicólogo'), ('Nutricionista')) as v (name)
where not exists (
    select 1 from public.professional_types pt
    where lower(pt.name) = lower(v.name) and pt.deleted_at is null
);
