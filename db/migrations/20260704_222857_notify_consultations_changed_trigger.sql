-- Migración: notify consultations changed trigger
-- Creada:    2026-07-04 22:28:57
--
-- Emite un pg_notify('consultations_changed', ...) en cada cambio de consultations.
-- Lo consume el WebSocket /ws/consultations del backend (real-time SOLO en local; en prod
-- el frontend usa Supabase Realtime). Inofensivo si no hay listener (prod): pg_notify sin
-- suscriptor es prácticamente un no-op y no interfiere con la replicación lógica de Supabase.
--
-- Idempotente: create or replace + drop trigger if exists.

create or replace function public.notify_consultations_changed()
returns trigger
language plpgsql
as $$
declare
    v_row public.consultations;
begin
    if tg_op = 'DELETE' then
        v_row := old;
    else
        v_row := new;
    end if;
    perform pg_notify(
        'consultations_changed',
        json_build_object(
            'op', tg_op,
            'id', v_row.id,
            'status', v_row.status,
            'assigned_doctor_id', v_row.assigned_doctor_id
        )::text
    );
    return null;  -- AFTER trigger: el valor de retorno se ignora
end;
$$;

drop trigger if exists trg_notify_consultations on public.consultations;
create trigger trg_notify_consultations
    after insert or update or delete on public.consultations
    for each row
    execute function public.notify_consultations_changed();
