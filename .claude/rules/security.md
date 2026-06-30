# Reglas de Seguridad (OWASP / IDOR / Datos Médicos)

Manejamos **PII médica** (pacientes, cédulas, teléfonos, motivos de consulta). La seguridad es
obligatoria, no opcional. Estas reglas son de cumplimiento estricto.

## 🔑 Autorización y RBAC (replicar las políticas RLS de Supabase)
Hoy la app Next.js protege los datos con **RLS en Supabase**. Esta API se conecta como dueño de la
base (RLS no aplica), por lo que **la autorización debe imponerse en la capa de servicios**,
replicando exactamente esas políticas. Mientras no exista JWT, ningún endpoint que exponga PII debe
publicarse a internet sin esta capa.

Roles: `patient | doctor | specialist | admin | super_admin`. Equivalencias a respetar:
- **is_staff** = `doctor | specialist | admin | super_admin` → puede leer pacientes y consultas.
- **is_admin** = `admin | super_admin` → puede reasignar, cambiar estados, revocar médicos.
- Un médico **revocado** (`active = false`) o no verificado (`verified = false`) pierde el acceso
  de inmediato (equivalente a `current_user_role()` devolviendo NULL).
- `set_my_role` solo permite `patient`/`doctor` (NUNCA escalar a admin/specialist desde el cliente).

## 🛡️ IDOR (Insecure Direct Object Reference) — OWASP A01
- **Nunca** confíes en un ID de la URL/payload para devolver o mutar un recurso sin verificar que el
  llamante tiene derecho sobre ese recurso.
- Un paciente con cuenta solo puede ver **sus propias** consultas (`patients.user_id = caller`).
- Un médico solo actualiza consultas **sin asignar o asignadas a sí mismo** (salvo admin).
- La verificación de pertenencia va en el **servicio**, junto a la query, no en el router.

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
