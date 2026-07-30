-- Migración: token secreto por usuario para el feed iCal de su agenda (suscripción webcal://).
-- Con este token el calendario del usuario (Google/Apple/Outlook) sondea GET /agenda/{token}.ics y
-- mantiene sus citas sincronizadas. De solo lectura; regenerable (rotar el uuid revoca la URL vieja).
-- Se llena on-demand la primera vez que el usuario pide su URL de calendario. Idempotente.

alter table public.users
    add column if not exists calendar_token uuid;

-- Único para el lookup del feed por token (parcial: solo filas que ya lo generaron).
create unique index if not exists ux_users_calendar_token
    on public.users (calendar_token) where calendar_token is not null;
