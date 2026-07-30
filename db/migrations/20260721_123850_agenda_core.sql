-- Migración: núcleo de Agenda — citas agendadas (fecha/hora futura) + cadena padre→hijas de
-- seguimiento + firma del médico al cerrar. Ver el plan del módulo Agenda.
-- No confundir con "Interconsulta" (segunda opinión en vivo). Idempotente.

-- 1) Columnas nuevas en consultations.
alter table public.consultations
    -- Fecha/hora agendada de la cita (null = consulta normal de cola en tiempo real).
    add column if not exists scheduled_at timestamptz,
    -- Idempotencia del recordatorio "30 min antes": se setea al enviar el email.
    add column if not exists reminder_sent_at timestamptz,
    -- Cadena de seguimiento: consulta padre de la que se derivó esta (patrón de patients.parent_id).
    add column if not exists parent_consultation_id uuid
        references public.consultations(id) on delete set null,
    -- Firma del médico al cerrar (dataURL PNG). Acto médico firmado; base para récipes (módulo futuro).
    add column if not exists close_signature text;

-- 2) Nuevo status 'scheduled' (cita agendada, aún no atendida) en el CHECK.
alter table public.consultations drop constraint if exists consultations_status_check;
alter table public.consultations add constraint consultations_status_check
    check (status in ('waiting', 'in_progress', 'referred_to_specialist', 'urgent_in_person',
                      'closed', 'cancelled', 'patient_no_show', 'closed_by_admin',
                      'contacted_whatsapp', 'scheduled'));

-- 3) Índices: agenda (citas por fecha) y cadena (hijas por padre).
create index if not exists ix_consultations_scheduled_at
    on public.consultations (scheduled_at) where scheduled_at is not null;
create index if not exists ix_consultations_parent
    on public.consultations (parent_consultation_id) where parent_consultation_id is not null;
