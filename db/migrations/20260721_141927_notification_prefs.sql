-- Migración: preferencias de notificación por usuario (para que el sistema no sea invasivo).
-- JSONB { "<evento>": { "push": bool, "email": bool } }. '{}' = todo activado (opt-out): un evento
-- o canal ausente se interpreta como habilitado. Ver src/services/notifications.py (catálogo +
-- should_send) y el centro de Ajustes del perfil médico. Idempotente.

alter table public.users
    add column if not exists notification_prefs jsonb not null default '{}'::jsonb;
