-- Migración: avisos_chat_presencia
-- Creada:    2026-10-16 20:04:40
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).


-- Estado de los avisos por correo del chat, que hasta ahora vivía en un `dict` en memoria del
-- proceso (`notifications._LAST_MAIL_NOTIFICATIONS`). Correcto para un solo uvicorn; con dos
-- réplicas cada una frenaba solo sus propios envíos, y un reinicio borraba el freno entero.
-- Mismo patrón que `consultations.reminder_sent_at`: la idempotencia de un correo se guarda
-- en la fila del caso, no en el proceso.
--
-- Nada de backfill: nacen en null y solo las escriben los mensajes posteriores al despliegue.
-- Un barrido retroactivo mandaría una ráfaga de avisos sobre hilos ya resueltos por otra vía.

alter table public.consultations
  add column if not exists patient_notice_sent_at timestamptz;

alter table public.consultations
  add column if not exists doctor_notice_sent_at timestamptz;

-- Aviso al paciente DIFERIDO porque estaba en línea cuando su médico escribió: el mensaje ya le
-- apareció por SSE, así que el correo solo tiene sentido si no lo lee. Lo manda el barrido
-- `send_pending_message_notices` pasados MESSAGING_MAIL_ONLINE_DELAY_SECONDS; leer lo cancela.
alter table public.consultations
  add column if not exists patient_notice_pending_at timestamptz;

comment on column public.consultations.patient_notice_sent_at is
  'Último aviso por correo de mensaje nuevo enviado al paciente en este hilo (anti-ráfaga).';
comment on column public.consultations.doctor_notice_sent_at is
  'Último aviso por correo de mensaje nuevo enviado al médico en este hilo (anti-ráfaga).';
comment on column public.consultations.patient_notice_pending_at is
  'Aviso al paciente diferido por estar en línea; lo resuelve el barrido o lo cancela la lectura.';

-- Índice parcial para el barrido: busca solo las filas con aviso pendiente, que son una
-- fracción mínima de la tabla. Sin él, el cron haría un seq scan de `consultations` cada minuto.
create index if not exists consultations_patient_notice_pending_idx
  on public.consultations (patient_notice_pending_at)
  where patient_notice_pending_at is not null;
