-- Migración: clinical ciphertext checks despues del backfill
-- Creada:    2026-09-23 13:49:11
--
-- ⚠️ NO está en db/migrations/ a propósito. Se mueve allí en un PR posterior, cuando en
-- producción ya se corrió `scripts/encrypt_clinical_data.py` y `--verify` da 0 pendientes.
-- Motivo: deploy.sh aplica las migraciones ANTES de cambiar la imagen. Si esto corriera en el
-- mismo deploy, rechazaría las escrituras de la API vieja (que aún guarda en claro) y cualquier
-- UPDATE sobre una fila legada (un CHECK se evalúa sobre la fila entera, aunque el UPDATE solo
-- cambie el `status`).
--
-- Qué hace: garantiza a nivel de base que en estas columnas solo entra texto cifrado por la
-- API. Un INSERT/UPDATE por SQL, por un script o por una API vieja con texto en claro falla.
-- Es la red que atrapa al próximo que escriba un motivo sin pasar por EncryptedText.
--
-- Guard: si queda algún valor en claro, aborta con un mensaje que dice qué correr. Así, en
-- un entorno local con datos restaurados de un backup viejo, `migrate` no deja la base a medias.

do $$
declare
  c record;
  pendientes bigint;
begin
  for c in
    select * from (values
      ('consultations', 'chief_complaint'),
      ('consultations', 'clinical_notes'),
      ('consultations', 'internal_note'),
      ('consultation_events', 'note'),
      ('interconsultations', 'note'),
      ('interconsultation_requests', 'chief_complaint'),
      ('interconsultation_requests', 'clinical_notes'),
      ('interconsultation_requests', 'closing_note'),
      ('patients', 'description'),
      ('patients', 'allergies'),
      ('prescriptions', 'medications'),
      ('prescriptions', 'instructions'),
      ('referrals', 'referred_to'),
      ('referrals', 'reason'),
      ('rest_notes', 'reason'),
      ('treatment_plans', 'plan'),
      ('follow_ups', 'notes'),
      ('messages', 'body')
    ) as t(tabla, columna)
  loop
    execute format(
      'select count(*) from public.%I where %I is not null and %I not like %L',
      c.tabla, c.columna, c.columna, 'enc:v1:%'
    ) into pendientes;
    if pendientes > 0 then
      raise exception
        '%.% tiene % valores sin cifrar. Corre scripts/encrypt_clinical_data.py antes de esta migración.',
        c.tabla, c.columna, pendientes;
    end if;

    execute format(
      'alter table public.%I drop constraint if exists %I',
      c.tabla, c.tabla || '_' || c.columna || '_cifrado'
    );
    execute format(
      'alter table public.%I add constraint %I check (%I is null or %I like %L)',
      c.tabla, c.tabla || '_' || c.columna || '_cifrado', c.columna, c.columna, 'enc:v1:%'
    );
  end loop;
end $$;
