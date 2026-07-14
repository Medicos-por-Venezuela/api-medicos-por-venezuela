-- Migración: harden audit_log immutability trigger
-- Creada:    2026-07-09 00:30:22
--
-- La excepción de inmutabilidad introducida en 20260707_014601 (para permitir el
-- ON DELETE SET NULL del FK actor_user_id -> users) era demasiado amplia: aceptaba
-- CUALQUIER UPDATE que pusiera actor_user_id en NULL sin tocar otros campos — es
-- decir, un UPDATE manual podía anonimizar el "quién" de una entrada de auditoría
-- (pérdida de no repudio) sin borrar el perfil.
--
-- Ahora se exige además:
--   * pg_trigger_depth() > 1  -> el UPDATE llega anidado desde otro trigger (el
--     RI interno de Postgres que ejecuta el SET NULL del FK). Un UPDATE manual
--     entra con depth = 1 y se rechaza.
--   * old.actor_user_id is not null -> solo tiene sentido anonimizar algo que
--     tenía actor.

create or replace function public.audit_log_block_write() returns trigger
language plpgsql as $$
begin
    if tg_op = 'UPDATE'
       and pg_trigger_depth() > 1
       and old.actor_user_id is not null
       and new.actor_user_id is null
       and new.id = old.id
       and new.action = old.action
       and new.resource is not distinct from old.resource
       and new.resource_id is not distinct from old.resource_id
       and new.metadata is not distinct from old.metadata
       and new.ip is not distinct from old.ip
       and new.correlation_id is not distinct from old.correlation_id
       and new.created_at = old.created_at
    then
        return new;
    end if;
    raise exception 'audit_log es inmutable: no se permite %', tg_op;
end;
$$;

-- TRUNCATE no dispara triggers de fila (el base es BEFORE UPDATE OR DELETE ... FOR EACH ROW),
-- así que era el único vector que quedaba para vaciar la bitácora. Se cierra con un trigger
-- de sentencia que reusa la misma función (para TRUNCATE ninguna rama del IF matchea -> raise).
drop trigger if exists trg_audit_log_no_truncate on public.audit_log;
create trigger trg_audit_log_no_truncate
    before truncate on public.audit_log
    for each statement execute function public.audit_log_block_write();

