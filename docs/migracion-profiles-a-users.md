# Informe: migración `profiles` → `users` (expand/contract, sin downtime)

> Estado: **plan aprobado, pendiente de ejecutar/probar.** Prod hoy es Next.js + Supabase directo;
> el backend ya está en EC2 pero el frontend aún no lo consume (cutover ~1 semana).

## 1. Objetivo

Unificar la identidad en una tabla **`users`** (cuenta) y dejar `doctors`/`patients` como extensiones
1:1. Hoy la tabla de cuentas se llama `profiles` (espejo 1:1 de `auth.users`). Este informe cubre solo
el **primer paso**: renombrar `profiles → users` **sin tirar la plataforma**.

## 2. Por qué un `RENAME` a secas rompe todo

El frontend (anon key) y la capa de seguridad de la BD nombran `profiles` **por su nombre**:

- **Frontend** (`@supabase/supabase-js`): ~10 páginas hacen `.from('profiles')` (lee y escribe).
- **Trigger de Auth**: `handle_new_auth_user()` hace `insert into public.profiles`.
- **RPC**: `set_my_role()` hace `update public.profiles`.
- **Helpers RLS** (security definer): `current_user_role()`, `current_user_specialty()`,
  `is_admin()`, `is_staff()` hacen `select ... from public.profiles`.
- **Políticas RLS**: `profiles_select_self_or_admin`, `profiles_insert_admin`, `profiles_update_admin`.

Un `alter table profiles rename to users` deja todo eso apuntando a un nombre inexistente → caída.

## 3. Estrategia: rename + **vista de compatibilidad** `security_invoker`

```
auth.users ──1:1── public.users   (la tabla real, antes 'profiles')
                        ▲
                        │  security_invoker = true  (respeta la RLS de users)
             public.profiles  ← VISTA sobre users, mismo nombre de siempre
```

- El **rename** conserva automáticamente **FKs, índices, RLS y triggers** (los sigue la tabla).
- La **vista `profiles`** mantiene vivo el nombre viejo para el frontend, el trigger de Auth y las
  funciones — **sin modificar ninguno**. Es una `select *` de una sola tabla → **auto-actualizable**
  (INSERT/UPDATE/DELETE se reescriben a `users`).
- `security_invoker = true` (Postgres 15+; prod y local corren **PG17** ✓) hace que la vista se
  ejecute con los privilegios de quien consulta → la **RLS de `users` se aplica** (sin esto, un view
  bypasearía la RLS).
- Requiere **re-otorgar grants** al frontend: los grants de la tabla **no** se heredan al view.
- **Sin Realtime que romper**: el frontend refresca por polling, no por `postgres_changes`.

## 4. Migración SQL (fase *expand*)

Crear con `python artisan "make:migration" "rename profiles to users with compat view"` y pegar:

```sql
-- === profiles -> users: rename + vista de compatibilidad ===
-- Transaccional. Con guardas para re-ejecución segura.

-- 1) Rename de la tabla. FKs, índices, RLS y triggers la siguen automáticamente.
do $$
begin
  if exists (
        select 1 from information_schema.tables
        where table_schema='public' and table_name='profiles' and table_type='BASE TABLE'
     ) and not exists (
        select 1 from information_schema.tables
        where table_schema='public' and table_name='users'
     ) then
    execute 'alter table public.profiles rename to users';
  end if;
end $$;

-- 2) Vista de compatibilidad con el nombre viejo (frontend directo, trigger de Auth,
--    set_my_role, current_user_role/is_admin/is_staff). security_invoker => aplica la RLS de users.
create or replace view public.profiles with (security_invoker = true) as
  select * from public.users;

-- 3) Grants que la tabla tenía y el view NO hereda.
grant select, insert, update, delete on public.profiles to anon, authenticated;
```

> Nota: el rename mueve nuestro trigger de coexistencia `trg_sync_user_roles_from_profile` a `users`
> — sigue funcionando (los INSERT del frontend/Auth vía la vista se reescriben a `users` y disparan
> el trigger de la tabla base).

## 5. Cambios en el backend (SQLAlchemy)

Apuntar los modelos a `users` (5 referencias):

| Archivo | Cambio |
| ------- | ------ |
| `src/models/profile.py:18` | `__tablename__ = "users"` (la clase puede seguir llamándose `Profile`) |
| `src/models/rbac.py:64` | `ForeignKey("profiles.id"` → `ForeignKey("users.id"` (user_roles.user_id) |
| `src/models/rbac.py:70` | idem (user_roles.assigned_by) |
| `src/models/audit_log.py` | `ForeignKey("profiles.id"` → `users.id` (actor_user_id) |
| `src/models/consultation.py:40` | `ForeignKey("profiles.id"` → `users.id` (assigned_doctor_id) |
| `src/models/consultation_event.py:27` | `ForeignKey("profiles.id"` → `users.id` (created_by) |

> Las FKs a nivel BD ya apuntan a `users` tras el rename; estos strings son solo el mapeo del ORM.

## 6. Plan de pruebas por fases

### Fase A — LOCAL (Docker), mañana primero — **riesgo cero**

1. `python artisan migrate` (aplica el rename+view al Docker local, que tiene datos reales restaurados).
2. Aplicar los cambios de modelos (sección 5).
3. `pytest -q` → los **114 tests** deben pasar sin tocar nada más.
4. Verificar la vista a mano:
   ```sql
   -- insertar "como el frontend" por el nombre viejo, debe caer en users:
   insert into public.profiles (id, full_name, role, role_chosen, active, verified)
     values (gen_random_uuid(), 'Test view', 'patient', true, true, true);
   select count(*) from public.users where full_name='Test view';  -- 1
   ```
5. Levantar la API local y probar `/health` + `GET /profiles` + un endpoint RBAC.

### Fase B — PROD (EC2), en ventana de bajo tráfico — con rollback listo

1. **Backup** de Supabase (Database → Backups o `pg_dump -Fc`).
2. `docker compose -f docker-compose.prod.yml exec api python artisan migrate` (aplica rename+view).
3. `git pull` + rebuild con los modelos apuntando a `users` + `up -d`.
4. Verificación (sección 7). Si algo falla → **rollback** (sección 8) en < 1 min.

## 7. Checklist de verificación (Fase B)

- [ ] **Frontend en vivo** (anon, vía la vista): login médico, `/admin/dashboard` lista perfiles,
      "Revocar acceso" (UPDATE) funciona.
- [ ] **Auth**: un signup nuevo crea fila en `users` (trigger de Auth vía la vista) **y** su rol en
      `user_roles` (nuestro trigger de coexistencia).
- [ ] **Backend**: `/health` = database up, `GET /profiles`, endpoints RBAC/catálogos.
- [ ] **RLS**: un paciente autenticado no ve perfiles ajenos (confirma que `security_invoker` aplica).

## 8. Rollback (inmediato)

```sql
drop view if exists public.profiles;
alter table public.users rename to profiles;
```

Y redeploy del backend con los modelos apuntando a `profiles` (o revertir al commit anterior).
Como el frontend nunca dejó de usar `profiles`, el rollback es transparente.

## 9. Contract futuro (NO mañana — semanas después, con ventana de rollback cumplida)

1. Reescribir `handle_new_auth_user`, `set_my_role`, `current_user_role`, `current_user_specialty`,
   `is_admin`, `is_staff` para nombrar `users` en vez de `profiles`.
2. `drop view public.profiles`.
3. `drop column role, specialty, medical_license, whatsapp_number, ...` de `users` (ya viven en
   `user_roles` / `doctors`).
4. Quitar el fallback a `profiles.role` en `src/services/authz.py` y el trigger de coexistencia.
5. Enlazar `doctors.user_id` y backfillear los 2390 (paso siguiente del modelo destino).

---

### Resumen de riesgo

| Fase | Riesgo | Reversible |
| ---- | ------ | ---------- |
| A (local) | ninguno | n/a |
| B (prod, rename+view) | bajo — el frontend sigue por la vista | sí, < 1 min |
| Contract (futuro) | medio — toca funciones y borra columnas | por partes |
