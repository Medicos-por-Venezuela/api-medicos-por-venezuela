# Datos de emergencia y dirección cifrada de extremo a extremo

Estado: aprobado (2026-09-20). Decisiones tomadas por el equipo:

1. **Cifrado E2E real con passphrase** para la dirección. Ni un programador ni un DBA pueden leerla:
   el servidor solo almacena texto cifrado y la clave privada nunca sale del navegador.
2. **Allowlist configurable** (env `ADDRESS_VIEWER_EMAILS`) para el super admin; por defecto
   `orianaramirez@gmail.com`.
3. **Ambos campos obligatorios** en el alta pública de pacientes.
4. El **teléfono de emergencia sí** entra a los reportes Excel (solo `super_admin`, permiso
   `reports.export`). La **dirección nunca** (el servidor no puede descifrarla).

## Modelo de amenaza y consecuencias aceptadas

- La dirección se cifra en el navegador del paciente con la **clave pública clínica** (X25519,
  sealed box). La **clave privada** vive envuelta (Argon2id + XSalsa20-Poly1305) en el bundle del
  frontend y se desbloquea en el navegador con una **passphrase** que solo conocen los médicos y la
  responsable. La API jamás recibe la passphrase, la clave privada ni el texto plano.
- **Pérdida de la passphrase = direcciones irrecuperables.** No hay escrow ni recuperación.
  Custodia: Oriana + 2 responsables, en gestor de contraseñas; copia sellada física. Rotación:
  nueva versión de clave (`v2:`) y re-cifrado de las filas existentes por una persona autorizada
  (herramienta cliente, fuera del alcance de esta entrega).
- La clave envuelta puede estar en un repo público sin riesgo: sin la passphrase no sirve. La
  passphrase debe ser aleatoria de ≥ 24 caracteres (el script la genera con CSPRNG).
- Cualquier médico puede llegar a ser tratante de cualquier caso, así que **todos los médicos
  reciben la passphrase**. Un médico que salga de la organización obliga a rotar la clave.

## Criptografía (contrato v1)

- Librería frontend: `libsodium-wrappers` (wasm) para el sealed box. El envoltorio de la clave
  privada usa WebCrypto nativo (sin dependencia extra).
- Clave pública clínica: 32 bytes X25519 en base64, configurada por env
  `NEXT_PUBLIC_CLINICAL_PUBLIC_KEY`.
- Clave privada envuelta: `NEXT_PUBLIC_WRAPPED_CLINICAL_KEY`, formato
  `v1:<salt_b64>:<iv_b64>:<ct_b64>` donde `ct = AES-256-GCM(private_key)` con la clave derivada
  de la passphrase por **PBKDF2-SHA256, 600.000 iteraciones** (WebCrypto). Se eligió PBKDF2 y no
  Argon2id porque `libsodium-wrappers@0.8.4` no expone `crypto_pwhash` en su build real (aunque
  sus tipos lo declaren) y la passphrase es aleatoria de 144 bits, así que el KDF no es la
  barrera principal.
- Dirección cifrada (lo único que se guarda en BD): `v1:<base64(crypto_box_seal(utf8(address), pk))>`.
  El prefijo de versión permite rotar a `v2:` sin ambigüedad.
- Desbloqueo: el navegador deriva la clave, abre el secretbox, valida que el par coincide con la
  pública y cachea la privada en memoria + `sessionStorage` (muere al cerrar la pestaña).
- Si las envs no están configuradas, `encryptAddress` lanza un error claro y el registro se bloquea
  con un aviso (nunca se guarda dirección en claro).

## Modelo de datos

```sql
alter table public.patients add column if not exists emergency_phone text;
alter table public.patients add column if not exists address_encrypted text;
comment on column public.patients.address_encrypted is
  'Dirección cifrada E2E (v1:base64 sealed box). La API NUNCA la descifra ni la loguea.';
```

Sin CHECK nuevo: las 848 filas existentes no tienen los campos y un CHECK las invalidaría. La
obligatoriedad se exige en el esquema Pydantic del alta pública (único escritor es la API).

## API

- `PatientCreate` (alta pública): `emergency_phone` requerido (5..30) y `address_encrypted`
  requerido (`^v1:[A-Za-z0-9+/=]+$`, ≤ 4000). Validador: el teléfono de emergencia, normalizado a
  dígitos, **debe ser distinto** de `phone_whatsapp` (422 si no).
- `DoctorPatientCreate`: `emergency_phone` opcional. Sin dirección.
- `PatientUpdate`: `emergency_phone` y `address_encrypted` opcionales.
- `PatientResponse`: agrega `emergency_phone`, pero **solo se serializa** para el equipo admin, el
  médico dueño del paciente de consultorio y el propio paciente; el resto del staff lo recibe en
  `null` (PII de contacto). `address_encrypted` **no aparece en ninguna respuesta** salvo el
  endpoint dedicado.
- `ConsultationDetailPatient`: agrega `emergency_phone`, **solo visible** para el equipo admin y
  el médico asignado al caso; en el listado y el detalle de un médico ajeno viaja en `null`.
- `ConsultationDetailResponse`: agrega `can_view_patient_address: bool`, calculado server-side:
  `true` si el email del principal está en la allowlist **o** `consultation.assigned_doctor_id ==
  principal.id`. (No se confía en el cliente.)
- **Nuevo** `GET /patients/{id}/address` (permiso `patients.read`), responde
  `{"address_encrypted": "v1:..."}`. Autorizado solo si: email en allowlist, o existe una consulta
  del paciente con `assigned_doctor_id == principal.id`. 403 en cualquier otro caso (incluido un
  admin que no esté en la allowlist). Escribe `audit_log` con acción
  `patient.address_revealed` (actor, patient_id, vía allowlist/tratante) antes de responder.
- Settings: `ADDRESS_VIEWER_EMAILS: str = "orianaramirez@gmail.com"` (coma-separado; property que
  devuelve un set en minúsculas). Documentar en `.env.example` y `.env.production.example`.
- Reportes: columna "Teléfono de emergencia" en pacientes y en consultas
  (`patient_emergency_phone`). La dirección no entra a ningún reporte.

## Frontend

- `lib/patientAddressCrypto.ts`: `encryptAddress`, `unlockClinicalKey`, `decryptAddress`,
  `lockClinicalKey`, `isClinicalKeyUnlocked`, `clinicalKeyConfigured`. Import dinámico de
  `libsodium-wrappers` (wasm, solo en el navegador).
- Registro de paciente: teléfono de emergencia (reusa `PhoneField`) y dirección (texto, 5..300),
  obligatorios en adulto y en representante del menor. El menor hereda el mismo teléfono y la misma
  dirección (se cifra una vez y se reutiliza el ciphertext). Refine de Zod: emergencia ≠ WhatsApp.
- Panel admin (`pages/admin/pacientes.tsx`): teléfono de emergencia en la celda Contacto.
- Detalle del médico (`pages/panel-medico/consulta/[id].tsx`): teléfono de emergencia visible;
  bloque "Dirección" solo si `can_view_patient_address`, con botón "Ver dirección" que pide la
  ciphertext al endpoint, la descifra en el navegador (si la clave está bloqueada, abre el modal de
  passphrase) y la muestra solo en memoria del componente.
- `components/UnlockClinicalKeyModal.tsx`: pide la passphrase, desbloquea y avisa del error sin
  filtrar datos.
- Términos (`pages/legal/privacidad.tsx`): sección 3 (qué se recoge y por qué: emergencias),
  sección 6 (quién ve el teléfono y quién la dirección) y sección 9 (cifrado E2E). Texto del
  checkbox de consentimiento en el registro: menciona la finalidad de emergencia y el cifrado.
- E2E: los specs de registro deben llenar los campos nuevos; se agrega cobertura del flujo
  dirección (médico asignado ve, otro no).

## Fases

1. Backend completo + tests.
2. Script `scripts/generate-clinical-keypair.mjs` y `docs/clave-clinica.md` (custodia y rotación).
3. `lib/patientAddressCrypto.ts` (escrito y revisado por el modelo principal, no delegado).
4. UI: registro, admin, detalle médico, términos, e2e.
5. Verificación cruzada y PRs.
