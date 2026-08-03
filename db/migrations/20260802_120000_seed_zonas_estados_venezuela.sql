-- Migración: las "zonas afectadas" pasan a ser simplemente "zonas", y el catálogo
-- deja de ser sectores del terremoto para ser los estados de Venezuela
-- (23 estados + Distrito Capital). Se conserva la tabla/endpoint affected_zones:
-- solo cambia el contenido y la etiqueta en la UI.
-- Creada:    2026-08-02
--
-- Idempotente: se puede correr dos veces sin duplicar ni re-borrar.

-- La lista canónica, escrita una sola vez (se usa para borrar lo viejo y sembrar lo nuevo).
-- ON COMMIT DROP: el runner envuelve cada migración en una transacción.
create temporary table _estados (nombre text primary key) on commit drop;

insert into _estados (nombre) values
    ('Amazonas'),
    ('Anzoátegui'),
    ('Apure'),
    ('Aragua'),
    ('Barinas'),
    ('Bolívar'),
    ('Carabobo'),
    ('Cojedes'),
    ('Delta Amacuro'),
    ('Distrito Capital'),
    ('Falcón'),
    ('Guárico'),
    ('La Guaira'),
    ('Lara'),
    ('Mérida'),
    ('Miranda'),
    ('Monagas'),
    ('Nueva Esparta'),
    ('Portuguesa'),
    ('Sucre'),
    ('Táchira'),
    ('Trujillo'),
    ('Yaracuy'),
    ('Zulia');

-- 1) Baja lógica de las zonas por sector previas ("La Guaira - Maiquetía", "Caracas - Este",
--    etc.). Soft delete, nunca DELETE: los pacientes ya registrados guardan el texto de la
--    zona en patients.affected_zone, así que el histórico no se rompe.
update public.affected_zones z
set status = 'deleted',
    deleted_at = now(),
    updated_at = now()
where z.deleted_at is null
  and not exists (
      select 1 from _estados e where e.nombre = z.name and e.nombre = z.state
  );

-- 2) Siembra de los estados. name = state para que el frontend los muestre como
--    "Miranda" y no "Miranda - Miranda" (ver fetchAffectedZoneCatalog en lib/api.ts).
insert into public.affected_zones (name, state, country, status)
select e.nombre, e.nombre, 'Venezuela', 'active'
from _estados e
where not exists (
    select 1 from public.affected_zones z
    where z.deleted_at is null
      and lower(z.name) = lower(e.nombre)
      and lower(z.state) = lower(e.nombre)
);

-- 3) Si un estado ya existía pero estaba inactivo, se reactiva (el listado público
--    solo devuelve status = 'active').
update public.affected_zones z
set status = 'active',
    updated_at = now()
where z.deleted_at is null
  and z.status <> 'active'
  and exists (select 1 from _estados e where e.nombre = z.name and e.nombre = z.state);
