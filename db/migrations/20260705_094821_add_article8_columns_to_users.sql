-- Migración: add article8 columns to users
-- Creada:    2026-07-05 09:48:21
--
-- Rellena un gap de schema preexistente: el ORM (src/models/profile.py) ya declara
-- `did_article_8`/`article_8_doc_path` (se leen/exponen en ProfileResponse) y prod las
-- tiene, pero NUNCA existió una migración que las creara — ni en este repo ni en el
-- supabase_schema.sql del frontend. Se ocultaba porque el flujo local viejo restauraba
-- un backup de prod que ya las traía físicamente. Al construir el schema desde cero
-- (Supabase local) quedó expuesto: `column users.did_article_8 does not exist`.
--
-- Va DESPUÉS del rename profiles->users (apunta a `users`, el nombre vigente a esta
-- altura de la migración history). Pendiente aparte: sincronizar este mismo add-column
-- en supabase_schema.sql del repo del frontend (fuente de verdad del schema "core").

alter table public.users add column if not exists did_article_8 boolean not null default false;
alter table public.users add column if not exists article_8_doc_path text;
