-- Migración: cerrar rpc heredadas y admin users
-- Creada:    2026-09-23 19:30:00
--
-- Hallazgos del Security Advisor de Supabase (splinter) sobre producción, 2026-09-23:
--
-- 1) Funciones SECURITY DEFINER ejecutables por `anon` vía /rest/v1/rpc/. Tres no comprueban
--    quién llama: con el id de una consulta, cualquiera marcaba "el paciente entró a la llamada"
--    o "prefiere WhatsApp" y creaba eventos en su historial (mark_patient_entered_call,
--    mark_patient_wants_whatsapp); con el id de un paciente, leía sus etiquetas de necesidades de
--    salud (patient_needs). El frontend ya no llama a NINGUNA RPC: todas las reemplazó la API
--    (set_my_role -> POST /profiles/me/finalize-role, admin_delete_patient -> DELETE /patients,
--    el heartbeat -> Presence, entered_call -> POST /consultations/{id}/entered-call). Así que
--    los roles del navegador pierden EXECUTE en todas, salvo las tres que las policies de RLS
--    evalúan con los privilegios de quien consulta (owns_patient, is_admin, is_staff): esas las
--    conserva `authenticated` —sin ellas se rompe el Realtime del panel— y las pierde `anon`.
--    Las funciones de trigger (handle_new_auth_user, sync_user_roles_from_profile) siguen
--    disparándose: Postgres no comprueba EXECUTE al disparar un trigger, solo al crearlo.
--    La API entra como dueña y no se ve afectada.
-- 2) `admin_users`: tabla heredada (con columna password_hash) que nadie usa —ni la API, ni
--    ninguna función ni policy—, con TODOS los privilegios para anon/authenticated y policies
--    `public` de INSERT (WITH CHECK true) y SELECT. Queda deny-all como las tablas clínicas. No
--    se borra: bloquearla es reversible y basta.
-- 3) search_path mutable en dos funciones de trigger: se fija a `public`.
--
-- Idempotente: revoke/grant repetibles, drop policy if exists; cada función se toca solo si
-- existe (to_regprocedure), porque una base restaurada de un backup viejo puede no tenerlas.

do $$
declare
  fn text;
  rol text;
begin
  -- 1a) Sin uso desde el navegador: fuera EXECUTE para public/anon/authenticated.
  foreach fn in array array[
    'public.mark_patient_entered_call(uuid)',
    'public.mark_patient_wants_whatsapp(uuid)',
    'public.mark_patient_waiting(uuid)',
    'public.patient_needs(uuid)',
    'public.mark_myself_online()',
    'public.update_my_specialty(text)',
    'public.get_case_doctor(uuid)',
    'public.admin_delete_patient(uuid)',
    'public.set_my_role(text,text,text,text,text)',
    'public.current_user_role()',
    'public.current_user_specialty()',
    'public.doctor_can_practice(uuid)',
    'public.handle_new_auth_user()',
    'public.sync_user_roles_from_profile()'
  ] loop
    if to_regprocedure(fn) is not null then
      execute format('revoke all on function %s from public', fn);
      foreach rol in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = rol) then
          execute format('revoke all on function %s from %I', fn, rol);
        end if;
      end loop;
    end if;
  end loop;

  -- 1b) Usadas por policies de RLS: solo `authenticated` (las policies son TO authenticated).
  foreach fn in array array[
    'public.owns_patient(uuid)',
    'public.is_admin()',
    'public.is_staff()'
  ] loop
    if to_regprocedure(fn) is not null then
      execute format('revoke all on function %s from public', fn);
      if exists (select 1 from pg_roles where rolname = 'anon') then
        execute format('revoke all on function %s from anon', fn);
      end if;
      if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute format('grant execute on function %s to authenticated', fn);
      end if;
    end if;
  end loop;

  -- 3) search_path fijo.
  foreach fn in array array[
    'public.audit_log_block_write()',
    'public.generate_consultation_code()'
  ] loop
    if to_regprocedure(fn) is not null then
      execute format('alter function %s set search_path = public', fn);
    end if;
  end loop;

  -- 2) admin_users deny-all.
  if to_regclass('public.admin_users') is not null then
    execute 'alter table public.admin_users enable row level security';
    execute 'drop policy if exists "public insert admin_users" on public.admin_users';
    execute 'drop policy if exists "public select admin_users" on public.admin_users';
    foreach rol in array array['anon', 'authenticated'] loop
      if exists (select 1 from pg_roles where rolname = rol) then
        execute format('revoke all on public.admin_users from %I', rol);
      end if;
    end loop;
  end if;
end $$;
