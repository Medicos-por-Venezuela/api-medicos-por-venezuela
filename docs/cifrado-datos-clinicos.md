# Cifrado de datos clínicos y acceso por necesidad de saber

Contrato técnico completo: [`tasks/cifrado-datos-clinicos/spec.md`](../tasks/cifrado-datos-clinicos/spec.md).
Este documento es el de **operación**: la clave, el despliegue, la rotación, la auditoría y el
contrato para el frontend.

## Qué cambia

- El contenido clínico se guarda **cifrado** (AES-256-GCM) por la API. En la base, en los
  backups, en el WAL de Realtime y en el SQL Editor de Supabase se ve `enc:v1:<kid>:<base64>`.
- La API solo **descifra** para quien tiene derecho sobre ese caso concreto:

| Quién | Ve |
|---|---|
| Paciente dueño | Su motivo y sus antecedentes/alergias. Nunca las notas del médico. |
| Médico tratante (asignado, habilitado) | Todo el contenido clínico del caso. |
| Invitado a interconsulta / especialista que tomó la solicitud | Todo, de ESE caso. |
| Médico habilitado, caso sin asignar en su cola | Motivo y antecedentes, para decidir si lo toma. |
| admin / super_admin | Nada clínico. Estado, prioridad, asignación, `nota_admin`, métricas y auditoría. |

Un admin que además ejerce como médico (rol `doctor` + ficha habilitada) recibe lo que le toca
**como médico** —los casos que tomó por la cola o por el claim—, nunca por ser admin. No puede
asignarse un caso a sí mismo por el PATCH administrativo.

- Cada lectura concedida queda en `audit_log` como `READ_CLINICAL_DATA`; cada intento denegado
  sobre un caso concreto, también (con `outcome = "denied"`).
- Correos y calendarios `.ics` ya no llevan texto clínico.

Columnas cifradas: `consultations.{chief_complaint, clinical_notes, internal_note}`,
`consultation_events.note`, `interconsultations.note`,
`interconsultation_requests.{chief_complaint, clinical_notes, closing_note}`,
`patients.{description, allergies}` y las tablas `prescriptions`, `referrals`, `rest_notes`,
`treatment_plans`, `follow_ups`, `messages`. La lista canónica es el código: toda columna
declarada `EncryptedText` en `src/models/`.

**No** cifradas, a propósito: `nota_admin` (es la nota del equipo admin; no escriban ahí datos
clínicos), `close_signature` (firma, nunca sale por la API), `category`/`needs_tags` (se filtran
en SQL) y `address_encrypted` (ya va cifrada de extremo a extremo con otro esquema).

## La clave

- Variable: `CLINICAL_DATA_ENCRYPTION_KEY` (32 bytes en base64). Solo en el entorno de la API
  (`.env.production` del EC2). **Nunca** en Supabase, en Amplify ni en el repo.
- Generar: `uv run python scripts/encrypt_clinical_data.py --generate-key`.
- **Perderla = perder todo el contenido clínico.** No hay recuperación. Custodia igual que la
  passphrase clínica (ver `docs/clave-clinica.md` del frontend): gestor de contraseñas de la
  organización + copia sellada física, con al menos dos custodios.
- Filtrarla = quien tenga además un volcado de la base puede leer todo. Si se sospecha, rotar.
- Los backups (`scripts/backup_supabase.sh`) quedan cifrados: guardar la clave **separada** de
  ellos. Un backup y su clave en el mismo sitio no protegen nada.

## Despliegue (orden obligatorio)

1. Generar la clave y ponerla en `.env.production` (y en el gestor de contraseñas).
2. `./deploy.sh`: aplica `20260923_134911_rls_tablas_clinicas_deny_all.sql` y arranca la API nueva.
   Desde ese momento todo lo que se escribe se guarda cifrado; lo viejo se sigue leyendo
   (en claro en la base, pero ya enmascarado para quien no tiene permiso).
3. Backfill de lo histórico, en horario de baja demanda (cada fila cifrada dispara un evento de
   Realtime y el panel de los médicos refresca). Todo corre en un contenedor efímero de la imagen
   recién desplegada (`run --rm`), con el mismo `.env.production` que la API, desde el directorio
   del repo en el EC2:
   ```bash
   cd ~/api-medicos-por-venezuela            # o donde esté el repo en el EC2
   C="docker compose -f docker-compose.prod.yml run --rm api python scripts/encrypt_clinical_data.py"

   # kid de la clave con la que arrancó la API (no es secreto):
   KID=$(docker logs mpv-api 2>&1 | grep -o 'kid=[0-9a-f]*' | tail -1 | cut -d= -f2); echo "$KID"

   $C --dry-run --expect-kid "$KID"                          # cuenta lo pendiente, no escribe
   $C --expect-kid "$KID" --operator tu.correo@dominio.org   # cifra (queda en audit_log)
   $C --verify --expect-kid "$KID"                           # exit 0 = nada en claro
   $C --vacuum                                               # borra de disco las versiones en claro
   ```
   - `--expect-kid` evita el peor error: cifrar producción con la clave de desarrollo (está en el
     repo). Además, el script se niega a usar esa clave contra una base remota.
   - `--operator` es obligatorio al escribir: la corrida queda en `audit_log` como
     `clinical_data.bulk_encrypt`, con una fila al empezar y otra al terminar (o al fallar).
   - Una fila que no descifra (clave ajena, manipulada) no aborta la corrida: se informa por id,
     sin contenido, y el script sale con 1.
   - Es idempotente: si se corta, se vuelve a lanzar el mismo comando.
4. Desplegar el frontend (rama `dev_aws`) con el marcador de confidencial y sin la edición de la
   nota del médico en el panel admin.
5. Desplegar la migración `20260923_214425_clinical_ciphertext_checks.sql` (PR aparte, hecho
   tras el backfill de prod del 2026-09-23). Añade `CHECK (col LIKE 'enc:v1:%')` en las 18
   columnas: desde ahí, ningún texto en claro puede volver a entrar a la base, ni por SQL. Si
   queda algo sin cifrar, la migración aborta y dice qué correr. No podía ir en el mismo deploy
   que la API: `deploy.sh` migra ANTES de cambiar la imagen, y el CHECK habría rechazado las
   escrituras de la API vieja y cualquier UPDATE sobre una fila legada.

Por qué el backfill es Python y no SQL: con `pgp_sym_encrypt(texto, 'clave')` la clave quedaría
en el texto de la consulta, en `pg_stat_statements`, en los logs de Postgres y en el historial
del SQL Editor. Justo donde no debe estar.

## Rollback

Si tras el backfill hay que volver a una API anterior al cifrado (que no sabe leer `enc:v1:`):

1. **Antes de nada, conservar la imagen con cifrado.** `deploy.sh` reconstruye siempre la misma
   etiqueta (`api-medicos-por-venezuela`): al desplegar la versión vieja, la nueva —la única que
   trae el script— desaparece.
   ```bash
   docker tag api-medicos-por-venezuela api-medicos-por-venezuela:cifrado
   KID=$(docker logs mpv-api 2>&1 | grep -o 'kid=[0-9a-f]*' | tail -1 | cut -d= -f2); echo "$KID"
   ```
2. Quitar los `CHECK` de texto cifrado (migración `20260923_214425`); el script se niega a
   descifrar mientras existan. Desde el SQL Editor de Supabase:
   ```sql
   do $$ declare r record; begin
     for r in select conrelid::regclass as t, conname from pg_constraint
              where conname like '%\_cifrado' loop
       execute format('alter table %s drop constraint %I', r.t, r.conname);
     end loop;
   end $$;
   ```
3. Desplegar la API anterior (`./deploy.sh <rama o commit anterior>`).
4. Descifrar todo con la imagen conservada, incluido lo que la API nueva escribió mientras tanto:
   ```bash
   D="docker run --rm --env-file .env.production api-medicos-por-venezuela:cifrado python scripts/encrypt_clinical_data.py"
   $D --decrypt --dry-run --expect-kid "$KID"
   $D --decrypt --yes --expect-kid "$KID" --operator tu.correo@dominio.org
   $D --decrypt --verify
   ```
   Queda en `audit_log` como `clinical_data.bulk_decrypt`. Deja la base otra vez en claro: es
   una medida de emergencia, no un estado para quedarse.

## Ensayo con copia de producción (2026-09-23)

Sobre una copia de prod del 2026-09-23 restaurada en local (954 consultas, 947 pacientes,
3.528 valores clínicos en claro), reproduciendo el deploy paso a paso:

| Paso | Resultado |
|---|---|
| Migración `20260923_134911` | 0,5 s. Fuera las policies `USING (true)` que prod SÍ tiene en `treatment_plans`/`messages`; Realtime conserva `id, status, assigned_doctor_id`. |
| API nueva con los datos aún en claro | Admin: 0 motivos/notas/antecedentes en 200 casos. Médico tratante: motivo y nota idénticos a prod. Otro médico: 403. Paciente: su motivo, sin notas. |
| Backfill (lotes de 200) | ~4 s para los 3.528 valores. `--verify` en 0. Sin triggers de UPDATE implicados. |
| Integridad | Los 3.528 valores descifrados coinciden byte a byte (SHA-256) con los de prod. |
| `VACUUM (FULL, ANALYZE)` | 81 ms. |
| `CHECK` post-backfill | Los 18 se crean sobre los datos de prod; un `UPDATE` en claro es rechazado. |
| Rollback `--decrypt` | ~4 s; los 3.528 valores vuelven idénticos al original. Re-cifrar después: idéntico. |
| Rotación de clave | La API sigue funcionando a mitad de la rotación; tras re-cifrar y quitar la clave vieja, todo idéntico. |

Hallazgos del ensayo:
- El backfill fila a fila tardaba 2 min 44 s (~46 ms por UPDATE). Se pasó a un UPDATE por lote
  (`unnest`), manteniendo la condición por fila que evita pisar escrituras concurrentes.
- El session pooler (5432) no respondió desde la red del ensayo; `pg_dump` funcionó por el 6543.
  `scripts/backup_supabase.sh` usa el 5432: comprobar que funciona **antes** del día del deploy.
- Perder la clave hace ilegibles los datos: en el ensayo se perdió una clave de rotación (solo
  estaba en una variable de la shell) y esa copia quedó irrecuperable. En prod, la clave va al
  gestor de contraseñas **antes** de cifrar nada.

## Rotación

1. Generar una clave nueva. En `.env.production`: la nueva en `CLINICAL_DATA_ENCRYPTION_KEY` y la
   vieja en `CLINICAL_DATA_ENCRYPTION_PREVIOUS_KEYS`. Reiniciar la API.
2. Correr el script (paso 3 de arriba): re-cifra todo lo que tenga otro `kid`.
3. `--verify` en 0 → vaciar `CLINICAL_DATA_ENCRYPTION_PREVIOUS_KEYS` y reiniciar.

## Auditoría

```sql
-- ¿Quién leyó el contenido clínico del caso X?
select created_at, actor_user_id, metadata->>'via' as via, metadata->>'outcome' as outcome, ip
from audit_log
where action = 'READ_CLINICAL_DATA'
  and (resource_id = 'X' or metadata->'ids' ? 'X')
order by created_at desc;
```

```sql
-- ¿Quién corrió el script de cifrado / descifrado masivo, y qué hizo?
select created_at, action, metadata->>'phase' as fase, metadata->>'operator' as operador,
       metadata->>'total' as filas, metadata->>'host' as host, correlation_id
from audit_log
where resource = 'clinical_data'
order by created_at desc;
```

Una corrida con `started` y sin `finished` ni `failed` es una corrida cortada (el proceso murió):
lo que alcanzó a hacer está escrito, lote a lote, y basta con relanzarla.

Se auditan como `denied` los 403 sobre un recurso concreto: detalle, cadena y eventos de una
consulta, interconsulta de una consulta, ficha de paciente y solicitudes de interconsulta.

Un detalle escribe una fila con `resource_id`. Un listado (el panel del médico) escribe **una**
fila con todos los ids en `metadata.ids`, para no escribir cien filas en cada refresco. El
índice GIN `audit_log_clinical_read_ids_idx` cubre la consulta de arriba.

**IP:** la API corre detrás de Caddy. `ip` es la del cliente porque uvicorn confía en el
`X-Forwarded-For` de Caddy, y solo en ese (`FORWARDED_ALLOW_IPS` = gateway de la red docker, puerto
publicado solo en `127.0.0.1`). Filas escritas antes de desplegar ese cambio tienen la IP del
proxy. Ver [proxy-e-ip-real.md](proxy-e-ip-real.md).

## Contrato para el frontend (DTO)

Los campos clínicos **no se enmascaran con un texto dentro del campo**: van en `null`, y la
respuesta dice por qué con `clinical_access`. Un marcador dentro del string no distingue
"confidencial" de "el paciente escribió eso", y si el admin guarda el formulario, el marcador
acabaría escrito como nota.

```jsonc
// GET /api/v1/consultations/{id} — vista del admin
{
  "id": "…", "code": "MPV-2026-0412", "status": "in_progress", "priority": "high",
  "assigned_doctor_id": "…", "specialty": "Cardiología", "queued_at": "…",
  "nota_admin": "Llamar al familiar si no contesta",
  "chief_complaint": null,
  "internal_note": null,
  "clinical_notes": null,
  "patient": { "full_name": "…", "cedula": "…", "description": null },
  "clinical_access": "none"
}

// La misma consulta, vista por el médico asignado
{
  "…": "…",
  "chief_complaint": "Dolor torácico opresivo desde hace 2 h",
  "internal_note": "ECG pendiente",
  "clinical_access": "full"
}
```

- `clinical_access`: `"full"` (tratante), `"summary"` (paciente dueño o médico viendo su cola:
  motivo y antecedentes, sin notas) o `"none"` (sin permiso).
- Regla de UI: si un campo clínico viene en `null` y `clinical_access` no es `"full"`, pintar
  **"[Información médica confidencial]"**, no un vacío. Si viene en `null` con `"full"`, está vacío.
- El admin no envía `chief_complaint`, `internal_note` ni `clinical_notes` en el PATCH: la API
  responde 403. Sí envía `status`, `priority`, `assigned_doctor_id`, `nota_admin`,
  `admin_seguimiento`.

## Marco normativo (resumen)

- **Confidencialidad / secreto médico** (Constitución de Venezuela art. 60, Código de
  Deontología Médica; HIPAA Privacy Rule "minimum necessary"): el acceso clínico queda limitado
  al equipo tratante y al propio paciente; el personal administrativo opera sin verlo.
- **Salvaguardas técnicas** (HIPAA Security Rule §164.312: control de acceso, auditoría,
  integridad, cifrado; ISO/IEC 27001 A.8.24 criptografía y A.8.15 registro; SOC 2 CC6/CC7):
  cifrado en reposo con clave fuera de la base, AAD por columna contra manipulación,
  `audit_log` inmutable con cada lectura, RLS deny-all para los roles del navegador.
- Esto es una medida técnica; la conformidad legal requiere además políticas, contratos
  (p. ej. con Supabase/AWS) y revisión por un profesional del área.
