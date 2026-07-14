# Reglas de Seguridad (OWASP / IDOR / Datos Médicos)

Manejamos **PII médica** (pacientes, cédulas, teléfonos, motivos de consulta). La seguridad es
obligatoria, no opcional. Estas reglas son de cumplimiento estricto.

## 🔑 Autorización y RBAC granular (modelo por permisos)
Esta API se conecta como **dueño** de la base (RLS de Supabase **no** aplica), así que la
autorización se impone en esta capa. El modelo es **RBAC granular multi-rol**: un usuario puede
tener **varios roles** y su permiso efectivo es la **unión** de los permisos de todos ellos.

**Tablas** (todas RLS deny-all; el backend es el único que las lee, ver `db/migrations/*_rbac_*`):
`users` (=`profiles`) → `user_roles` (N:M, con `revoked_at` soft-revoke) → `roles` →
`role_permissions` (N:M) → `permissions`. Más `audit_log` (append-only, ver abajo).

**Cómo se aplica en el código (NO reinventar):**
- El JWT de Supabase da el `sub` = id del perfil. `get_current_principal` (en `src/core/security.py`)
  llama a `authz.load_authz(db, user_id, fallback_role)` que devuelve `(roles, permissions)`
  efectivos y los cuelga del `Principal`.
- **Proteger un endpoint = una sola línea:** `_: Principal = Depends(require_permission("recurso.accion"))`.
  El factory `require_permission(code)` (en `security.py`) exige ese permiso y devuelve 403 si falta.
  **No** compares roles a mano en routers ni services; siempre por **permiso**.
- `Principal.has_permission(code)` = `self.active and code in self.permissions` → un usuario
  **revocado** (`active=false`) pierde **todos** los permisos al instante.
- **Backfill/coexistencia:** si un usuario aún no tiene filas en `user_roles`, `load_authz` cae al
  `profiles.role` (mapeando el legacy `specialist → doctor`). `specialist` **ya no es un rol**: todo
  médico es `doctor`.

**Roles sembrados** (`patient | doctor | admin | super_admin`) y sus permisos (18, ver el seed
`db/migrations/*_seed_rbac_*`): `patient` = 0 permisos de staff (solo ve lo suyo por pertenencia);
`doctor` = consultas (read/write/close) + cola (read/take) + patients.read + doctors.read;
`admin` = todo lo operativo + `catalogs.manage` + `roles.assign` + `audit.read`;
`super_admin` = **todos** los permisos (cross-join en el seed).

**Nuevos permisos → sembrarlos en una migración**, nunca a mano. Añade la fila en `permissions`,
mapéala a los roles en `role_permissions`, y protege el endpoint con `require_permission("...")`.

**Catálogos** (`specialties`, `affected_zones`, `professional_types`): el **listado es público**
(lo usa el registro de médicos/pacientes del sitio); todo el resto del CRUD exige `catalogs.manage`
(solo admin/super_admin).

**`is_staff`/`is_admin` son residuo legacy:** `is_staff` = rol staff + `active` + `verified`;
`is_admin` = admin/super_admin + `active`. Solo quedan en 2 endpoints de `profiles` (presencia y
detalle). Para código nuevo usa **siempre** `require_permission`, no estos flags.

- `set_my_role` solo permite `patient`/`doctor` (NUNCA escalar a admin desde el cliente).

## 🧾 Auditoría inmutable (no repudio)
Toda acción sensible (asignar/revocar rol, y las que se agreguen) se registra en `audit_log` vía
`src/services/audit.py::log_action` — **append-only y sin commit propio**: la entrada se persiste en
la misma transacción del caller (si la acción hace rollback, no queda audit huérfano). La tabla es
**inmutable a nivel de BD**: un trigger `before update or delete` (por fila) lanza excepción, y un
trigger `before truncate` (por sentencia) cierra el único vector restante — al proteger una tabla
con triggers de fila, recuerda que **TRUNCATE no los dispara**: necesita su trigger de sentencia.
Se lee con `GET /audit-log` (permiso `audit.read`). Cuando escribas una acción auditable, llama a
`log_action` dentro de la misma transacción; **no** hagas UPDATE/DELETE/TRUNCATE sobre `audit_log`.

## 🛡️ IDOR (Insecure Direct Object Reference) — OWASP A01
- **Nunca** confíes en un ID de la URL/payload para devolver o mutar un recurso sin verificar que el
  llamante tiene derecho sobre ese recurso.
- Un paciente con cuenta solo puede ver **sus propias** consultas (`patients.user_id = caller`).
- Un médico solo actualiza consultas **sin asignar o asignadas a sí mismo** (salvo admin).
- La verificación de pertenencia va en el **servicio**, junto a la query, no en el router.
- **La pertenencia cubre TODOS los caminos de mutación del recurso, incluidos sub-recursos.**
  Lección (review 2026-07-14): blindamos PATCH y `/close` con `_ensure_can_manage` pero
  `POST /{id}/events` quedó fuera — cualquier doctor podía inyectar un evento `closed` falso en
  el historial de otro. Al añadir un guard de pertenencia, `grep` por TODAS las rutas que mutan
  ese recurso o sus hijos (eventos, notas, adjuntos…) y aplícalo en cada una; el permiso RBAC
  (`consultations.write`) autoriza la *acción*, no el *objeto*.
- **Campos server-only en esquemas `*Update` llevan guarda explícita.** Que un campo esté en el
  schema no significa que el cliente pueda escribirlo: si `*Create` lo declara server-only (p.ej.
  `doctor_id`, que asigna el backend/cola), el `*Update` debe rechazarlo para no-admins — si no,
  el "mass assignment" entra por la puerta del PATCH aunque `extra="forbid"` esté puesto.

## ⚔️ Concurrencia: transiciones de estado SIEMPRE con escritura condicional
- **Prohibido read-then-write para tomar/finalizar/asignar recursos disputados.** Leer la fila,
  validar en Python y hacer `setattr + commit` deja una ventana donde dos actores ganan a la vez
  (el último pisa al primero **en silencio**, con 200 para ambos).
- El patrón correcto ya existe: `claim_consultation` hace `UPDATE ... WHERE assigned_doctor_id
  IS NULL` y trata `rowcount == 0` como 409 — **la base elige al único ganador**, no la app.
- Corolario (review 2026-07-14): un no-admin **no asigna consultas por PATCH** (solo liberar la
  suya o no-op); tomar es exclusivamente vía el claim atómico. Si un flujo nuevo necesita otra
  transición disputada (cerrar, reasignar), escribe con `UPDATE ... WHERE <estado esperado>` y
  verifica `rowcount`, nunca con el objeto leído.

## 🧹 Minimización y manejo de PII
- Guarda solo lo operativo. No registres conversaciones completas de consultas.
- **Nunca** loguees PII (nombres, cédulas, teléfonos, notas clínicas) ni la pongas en mensajes de
  error de cara al cliente.
- Los **backups** (`backups/`) contienen PII real: están en `.gitignore` y **jamás** se versionan
  ni se suben a servicios externos.
- `internal_note` / `clinical_notes` son de staff: no exponerlas a pacientes.

## 🚫 Protección de Producción
- **No** hagas INSERT/UPDATE/DELETE de prueba contra Supabase (producción). Verifica contra el
  Postgres local (Docker). Toda escritura a producción se hace solo con cambios revisados.

## ✅ Validación de entrada y salida
- Valida TODO input con Pydantic v2 (`EmailStr`, `Field(min_length=...)`, tipos `uuid.UUID`).
- Usa esquemas `*Response` para no filtrar campos internos por accidente (no devuelvas el modelo
  ORM crudo).
- **Mass assignment:** `model_config = ConfigDict(extra="forbid")` obligatorio en todos los
  esquemas `*Create` y `*Update`.
- **XSS:** aunque FastAPI sirve JSON, rechaza caracteres peligrosos (`<script>`) en campos de
  texto libre (notas, observaciones) vía validadores Pydantic o `max_length`.
- **SQL Injection:** prohibido usar `text()` con concatenación de strings. Usa siempre el ORM
  o parámetros enlazados (`bindparam`). Si `text()` es necesario, el valor nunca viene del cliente.
- **CSRF:** con JWT en `Authorization: Bearer` no hay riesgo de CSRF. Si se migra a cookies:
  `HttpOnly`, `Secure` y `samesite="lax"` son obligatorios.

## 🔐 Secretos y errores
- Secretos solo por variables de entorno (`.env` gitignored / gestor de secretos). Nunca hardcodear
  credenciales, claves de servicio ni `service_role`.
- Los manejadores globales (`src/core/exceptions.py`) traducen errores de BD a respuestas genéricas:
  **no** filtres SQL, stack traces ni nombres de constraints al cliente.
- CORS restringido por entorno (`BACKEND_CORS_ORIGINS`): `*` solo en desarrollo.

## 📦 Gestión de CVEs y auditoría
- Antes de agregar cualquier dependencia nueva: `uv run pip-audit` (o `pip-audit` directo) para
  verificar que no introduce vulnerabilidades conocidas.
- Los accesos denegados (401, 403) y errores de validación repetitivos (422) se loguean con nivel
  `WARNING` incluyendo el `user_id` (nunca PII) para integración con IDS externo (Fail2Ban/Datadog).
- Rate limiting en endpoints públicos (pendiente infra: `slowapi` + Redis o API Gateway).
