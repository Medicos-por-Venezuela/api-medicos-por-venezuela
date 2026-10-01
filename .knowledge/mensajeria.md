# Módulo Mensajería — contexto, acuerdos con el cliente y decisiones

Fuente: hilo del proyecto en Workana «Kelly Creativo - Desarrollo y Optimización Web para
Plataforma de Telemedicina con Chat y Mensajería Integrada» (18 → 29 de septiembre de 2026) y
lectura del código el 2026-09-29. Este archivo describe **lo acordado y lo que hay**; lo que se
construya se documenta aquí cuando exista, no antes. Spec y tareas:
`tasks/mensajeria-medico-paciente/`. Reglas: `.claude/rules/mensajeria.md`.

## Quién es quién

| Persona | Rol | Dónde |
|---|---|---|
| Adarvelys Valor | Contratante en Workana, coordinación | Ecuador |
| Ori (Oriana) Ramírez | Decide producto y flujo; pide la funcionalidad | Estados Unidos |
| Leonardo Alvarado | Desarrollador principal de ambos repos (todos los merges recientes) | — |
| Kelly (Kelly Creativo) | Freelancer contratada para este módulo | — |

Contrato: por horas en Workana, **USD 12,00/h**, sin límite semanal, comisión 20 %. Autorizado
el 2026-09-28. Estimación enviada: Fase 1 ≈ 20 h, Fase 2 ≈ 20 h, a confirmar tras revisar el
código (se ofreció 1–2 h de revisión sin costo). Se reportan horas por entrega en Workana.

## Lo que pidió Ori (21-sep, reenviado por Adarvelys)

1. Médico y paciente deben poder comunicarse y **agendar una cita** si quieren verse en persona.
2. Ideal: los mensajes le llegan al paciente por WhatsApp y al médico en la página.
3. Mínimo inmediato: el médico se comunica **solo desde la página**, con un **inbox del médico**.
4. Luego: el médico escribe en la página y el paciente responde por WhatsApp; protege el número
   del médico.
5. **No cambiar el flujo actual, agregar.** La videollamada actual «funciona no todo el tiempo»
   pero se deja hasta tener mejor plataforma de video.
6. «La única cosa que necesito que resolvamos: el flujo de los mensajes, la vaina de WhatsApp.»

## Acuerdos técnicos (resumen de Kelly del 28-sep, sin objeción del cliente)

- Objetivo: mensajería **asíncrona**. El médico escribe desde la web; el paciente responde desde
  su WhatsApp. La plataforma es el intermediario: nadie ve el teléfono del otro.
- WhatsApp por la **API Cloud oficial de Meta**. Gateway por QR descartado para producción (riesgo
  de bloqueo del número en salud). Ventana de 24 h y plantillas de utilidad aprobadas.
- Se reutiliza: SSE de la sala de espera (se suma el evento de mensaje), preferencias de
  notificación por evento, Mailtrap para correo, Jitsi intacto (enlace referenciado en el hilo).
- Se construye: tablas de mensajería (hilo por consulta, mensaje, estado de entrega y lectura),
  buzón del médico, conector con webhooks de Meta, registro de consentimiento del paciente.
- Fase 1 = buzón web del médico. Fase 2 = puente bidireccional con WhatsApp.

## Aclaraciones posteriores del cliente

- **Mailtrap es producción**: «no está solo como pruebas, se usa, se reciben y envían correos por
  ahí» (28-sep). No tratarlo como sandbox. En local, fijar `MAILTRAP_INBOX_ID` antes de probar
  (riesgo R1 abierto en `tasks/interconsulta-asincrona/todo.md`).
- Repos públicos; acceso concedido el 29-sep. Videollamada con Ori y Adarvelys pactada para el
  30-sep por el meet de Workana, horario sin fijar.

## Lo que hay en el código (2026-09-29)

- `messages` existe en BD y ORM (`src/models/clinical.py::Message`), vacía, RLS deny-all, cuerpo
  obligatoriamente cifrado (CHECK `20260923_214425`). Sin servicio, router, esquema ni test.
- Correo: `services/mail.py` + `mail_layout.py` + `notifications.py` (catálogo
  `NOTIFICATION_EVENTS`, opt-out) + `registration_mail.py`. Todo best-effort.
- SSE para el paciente: `GET /consultations/{id}/waiting-room/stream`. Realtime de Supabase
  para el panel del médico (refetch por señal mínima). Sin WebSocket.
- Sin push real. El frontend añadió `lib/firebase.ts` (FCM) el 2026-09-28 sin usarlo aún.
- Token de consulta sin sesión: `src/core/consultation_token` (paciente anónimo).
- Decisión de producto previa, que este módulo **reemplaza**: «la plataforma hace el match, no la
  conversación» (`.knowledge/interconsultas.md`) y «no registres conversaciones completas»
  (`security.md`). La nueva regla vive en `.claude/rules/mensajeria.md`.

## Decisiones tomadas para la spec (revisables con el cliente)

1. **Hilo = consulta.** No se crea tabla de hilos; se amplía `messages`.
2. **Paciente sin cuenta** lee y responde por web mediante el token de consulta (mismo
   mecanismo que la sala de espera); con cuenta, desde `/mi-caso`. En Fase 2, además, por WhatsApp.
3. **El aviso por correo nunca lleva el texto**; solo «tienes un mensaje nuevo» y el enlace.
4. **El mensaje de WhatsApp al paciente sí lleva el texto del médico** dentro de la ventana de
   24 h; fuera de ella, plantilla con enlace. Requiere consentimiento explícito registrado.
5. **Admin**: ve conteos y estados del hilo, nunca cuerpos (coherente con el cifrado clínico).
6. **Cerrar la consulta no cierra el hilo de inmediato**: el paciente puede responder durante una
   ventana (a definir, propuesta: 72 h) y el médico siempre puede leer.

## Preguntas abiertas (bloquean la tarea que las cita)

- **P1 Tarifa.** Propuesta y Workana: 12 USD/h; el resumen del 28-sep dice «USD 10/hora». Confirmar
  12 en la videollamada.
- **P2 Meta Business.** ¿Tienen cuenta verificada y número para la plataforma? Si no, Fase 2
  arranca contra el número de prueba de Meta Developer y la plantilla se solicita de inmediato
  (la aprobación tarda días).
- **P3 Chat en tiempo real dentro de la web.** La publicación dice «chat interno»; Ori describe
  algo asíncrono tipo buzón. Se asume **buzón** (mensajes persistidos, tiempo real solo como
  aviso). Confirmar.
- **P4 ¿Paciente responde por web en Fase 1?** Se asume que sí (cuenta o token). Confirmar que no
  esperan solo lectura.
- **P5 Ventana tras cerrar la consulta** (decisión 6). Confirmar 72 h u otra.
- **P6 Agendar cita presencial** (punto 1 de Ori). Existe módulo Agenda (`agenda.md`) para citas
  por video. ¿Quieren cita presencial como tipo nuevo o basta con acordarla por mensajes?
  Fuera del alcance de las 40 h salvo que lo prioricen.
- **P7 Correo al paciente** cuando no tiene email (anónimos): solo WhatsApp (Fase 2) o pedir email
  en el registro. Hoy `patients.email` es opcional.
