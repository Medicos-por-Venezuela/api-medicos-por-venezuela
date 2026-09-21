# Plan — datos de emergencia y dirección cifrada

Contrato completo en [spec.md](spec.md). Resumen de pasos:

## Backend (`api-medicos-por-venezuela`)

1. Migración `db/migrations/AAAAMMDD_HHMMSS_patients_emergency_contact_and_address.sql`:
   `emergency_phone`, `address_encrypted` + `comment on column`. Idempotente.
2. `src/models/patient.py`: dos columnas nuevas (nullable).
3. `src/schemas/patient.py`: campos y validador emergencia ≠ whatsapp; `address_encrypted` fuera de
   `PatientResponse`.
4. `src/schemas/consultation.py`: `emergency_phone` en `ConsultationDetailPatient`;
   `can_view_patient_address` en `ConsultationDetailResponse`.
5. `src/core/config.py`: `ADDRESS_VIEWER_EMAILS` + property; `.env.example` y
   `.env.production.example`.
6. `src/services/patients.py`: `patient_address_for_viewer` (gate + audit).
7. `src/routers/patients.py`: `GET /{id}/address`; `emergency_phone` en respuestas staff.
8. `src/routers/consultations.py`: calcular `can_view_patient_address` en el detalle.
9. `src/services/reports.py`: columna de teléfono de emergencia (pacientes y consultas).
10. Tests: alta pública (422 faltantes, 422 iguales, 201 ok), gate del endpoint de dirección
    (tratante 200, otro médico 403, allowlist 200, admin no allowlist 403, audit escrito),
    `can_view_patient_address` en el detalle, `address_encrypted` nunca en listados/detalle,
    reportes con teléfono de emergencia y sin dirección.
11. `ruff` + `pytest` de los archivos tocados.

## Frontend (`medicos-por-venezuela`)

12. `scripts/generate-clinical-keypair.mjs` + `docs/clave-clinica.md` (custodia, pérdida, rotación).
13. `lib/patientAddressCrypto.ts` (lo escribe el modelo principal) + `libsodium-wrappers`.
14. `components/UnlockClinicalKeyModal.tsx`.
15. Registro de paciente (adulto y menor): campos obligatorios, refine y cifrado antes del POST.
16. `pages/admin/pacientes.tsx`: teléfono de emergencia en Contacto.
17. `pages/panel-medico/consulta/[id].tsx`: teléfono + bloque Dirección condicionado a
    `can_view_patient_address`.
18. `pages/legal/privacidad.tsx` + texto del checkbox de consentimiento.
19. Tipos y clientes: `lib/patients.ts`, `lib/consultations.ts`, `lib/admin.ts`.
20. E2E: llenar campos nuevos en los specs de registro; cobertura del flujo dirección.
21. `tsc`, `lint`, `build` y `changeslog.md`.

## Operación (lo hace el equipo, no el código)

22. Correr el script de claves, guardar la passphrase en custodia (Oriana + 2), configurar
    `NEXT_PUBLIC_CLINICAL_PUBLIC_KEY` y `NEXT_PUBLIC_WRAPPED_CLINICAL_KEY` en Amplify/entornos.
23. Desplegar backend y luego frontend (el backend acepta los campos desde ya; el frontend viejo no
    los manda y el alta pública fallará hasta que ambos estén desplegados: coordinar la ventana).
