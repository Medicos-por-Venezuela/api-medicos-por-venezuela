-- Migración: allow actor_user_id set null on audit_log immutability trigger
-- Creada:    2026-07-07 01:46:01
--
-- audit_log.actor_user_id referencia profiles/users ON DELETE SET NULL, pero el
-- trigger de inmutabilidad bloqueaba TODO UPDATE -- incluida esa propia acción del
-- FK. Resultado: borrar cualquier profile que alguna vez hizo una acción auditada
-- (tomar un caso, cerrar/borrar una consulta, asignar un rol...) fallaba con
-- "audit_log es inmutable". Ahora el trigger permite únicamente el UPDATE que
-- pone actor_user_id en NULL sin tocar ningún otro campo (lo que hace el FK);
-- todo lo demás (y todo DELETE) sigue bloqueado.

create or replace function public.audit_log_block_write() returns trigger
language plpgsql as $$
begin
    if tg_op = 'UPDATE'
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

