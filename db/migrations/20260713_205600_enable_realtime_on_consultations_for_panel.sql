-- Migración: enable realtime on consultations for panel
-- Creada:    2026-07-13 20:56:00
--
-- El panel médico pasa de long-polling a Realtime (WebSocket): se suscribe a los cambios de
-- `consultations` para ver las consultas que entran/cambian en vivo. Realtime es el único
-- acceso directo a Supabase que se conserva (junto con Auth); los DATOS siguen viniendo del
-- backend (el evento Realtime solo dispara un refetch al backend).
--
-- Dos cosas hacen falta para que `postgres_changes` funcione con RLS:
--   1. REPLICA IDENTITY FULL: para que Realtime evalúe la RLS (consultations_select_staff)
--      sobre la fila completa en UPDATE/DELETE, no solo sobre la PK.
--   2. La tabla debe estar en la publicación `supabase_realtime`.
--
-- Idempotente: REPLICA IDENTITY es declarativo y el ADD TABLE se protege con IF NOT EXISTS.

alter table public.consultations replica identity full;

do $$
begin
    if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'consultations'
    ) then
        alter publication supabase_realtime add table public.consultations;
    end if;
end $$;
