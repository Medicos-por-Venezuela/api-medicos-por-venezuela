# Implementation Plan: comando de campaña para pedir la cédula

> Spec: [`spec.md`](./spec.md) · Tareas: [`todo.md`](./todo.md)

## Overview

Un comando de artisan que escribe a los ~2846 médicos antiguos sin cédula pidiéndoles que la
completen donde ya se valida contra SACS/FPV. **Dry-run por defecto**; enviar exige `--enviar`.

No se toca `doctors.verified` ni el flujo del panel: ambos ya funcionan. Esto solo añade alcance.

## Architecture Decisions

**1. El flag peligroso es el que hay que escribir.**
`--enviar` en vez de `--dry-run`. Un comando que manda 2846 correos no debe poder dispararse por
omisión ni por un `Enter` de más en el historial de la shell. El caso seguro es el silencio.

**2. Se revalida en el envío, no se confía en la lista.**
Entre el dry-run y el envío pueden pasar horas y el médico puede haber completado su cédula. Cada
destinatario se relee justo antes de escribirle. Molestar a alguien que ya hizo lo que le pedías es
la forma más rápida de que ignore el siguiente correo.

**3. Tabla de registro, no un log.**
`credential_reminders` con índice único. Un fichero de log no permite reintentar un lote fallido sin
duplicar, ni reenviar solo a quien no completó, ni medir conversión. La tabla sí, y es barata.

**4. Se integra en las preferencias existentes, no las esquiva.**
`credential_reminder` entra en `NOTIFICATION_EVENTS` y pasa por `should_notify()`. Saltarse el
sistema de preferencias "porque este correo es importante" es exactamente cómo se erosiona la
confianza en un canal.

**5. Solo se registra lo que Mailtrap aceptó.**
`send_mail` devuelve `bool` y es best-effort. Registrar antes de saber el resultado significaría dar
por escrito a gente que nunca recibió nada, y que además quedaría excluida del reintento.

**6. Migración primero, y sola.**
La tabla es aditiva y no depende del comando. Mergeable por su cuenta, lo que deja el PR del comando
más pequeño y reversible sin tocar el esquema.

## Dependency Graph

```
T1  migración: tabla credential_reminders
     │        ← aditiva, mergeable sola
     │
     ├── T2  evento credential_reminder en NOTIFICATION_EVENTS
     │        │
     │        └── T3  servicio: seleccionar destinatarios + segmentar
     │                 │
     │                 ├── T4  comando artisan (dry-run + --enviar)
     │                 │        │
     │                 │        └── T5  tests
     │                 │
     │                 └── T6  plantilla del correo   ← necesita tu visto bueno
```

`T6` es el único que no puedo cerrar solo: el texto lo tienes que aprobar tú.

## Task List

### Fase 1: Cimientos

- [ ] **T1** — Migración `credential_reminders`
- [ ] **T2** — Evento `credential_reminder` en el catálogo de notificaciones

### Checkpoint A — se puede registrar y respetar preferencias

- [ ] `artisan migrate` aplica limpio y `migrate:status` la lista
- [ ] El evento aparece en `GET /notification-prefs` y se puede desactivar
- [ ] `pytest` verde

### Fase 2: Selección y comando

- [ ] **T3** — Servicio de selección de destinatarios, con segmentos y exclusiones
- [ ] **T4** — Comando `doctors:pedir-cedula`, dry-run por defecto

### Checkpoint B — el comando enseña sin enviar

- [ ] Sin `--enviar` imprime el desglose y **no llama a `send_mail`** (test, no inspección)
- [ ] Los conteos del dry-run cuadran con una consulta SQL hecha a mano
- [ ] Revisar contigo antes de tocar el correo

### Fase 3: Correo y red de seguridad

- [ ] **T5** — Tests del comando
- [ ] **T6** — Plantilla del correo *(requiere tu aprobación del texto)*

### Checkpoint C — listo para el ensayo

- [ ] Los 9 criterios del spec, uno a uno
- [ ] Ensayo contra el inbox de Mailtrap con `--limite 5`
- [ ] Conteo de `doctors.verified` idéntico antes y después
- [ ] **El envío real a destinatarios reales lo autorizas tú, no se hace aquí**

## Risks and Mitigations

| Riesgo | Impacto | Mitigación |
| --- | --- | --- |
| **Enviar dos veces a la misma persona** | **Alto** — quema el canal | Tabla de registro con índice único + revalidación en el envío. Test de idempotencia |
| **Enviar sin querer durante el desarrollo** | **Alto** — irreversible | `--enviar` explícito; sin él ni se importa el envío. `mail_enabled()` es False sin token, así que local y tests no envían jamás |
| Ráfaga de ~2800 correos daña la reputación del dominio | Medio | `--limite` obligatorio en el primer lote, espaciado entre envíos, y lotes a lo largo de días |
| El médico completó entre el dry-run y el envío | Medio — molesta a quien ya cumplió | Se relee la ficha en el momento del envío (decisión 2) |
| Un fallo a mitad del lote deja el estado a medias | Medio | Solo se registra lo que Mailtrap aceptó; el resto entra en el siguiente lote sin duplicar |
| Las cifras del spec salen del restore local, no de prod | Bajo | El comando las recalcula en vivo; el dry-run es la fuente de verdad antes de cada lote |
| Escribir a cuentas revocadas o borradas | Bajo | Excluidas en la selección, y contabilizadas en el resumen para que se vean |

## Parallelization

- **Secuencial:** T1 → T2 → T3 → T4 → T5
- **En paralelo:** T6 (el texto) se puede redactar desde el principio; solo bloquea el ensayo final

## Out of Scope

- **Degradar `doctors.verified`.** Desde #72 impide atender. Es una decisión de producto, posterior
  a la campaña y con fecha límite anunciada.
- **El barrido de SACS.** Solo 109 fichas tienen cédula y ya pasaron por el verificador. Antes de
  cualquier barrido que escriba en `verified` hay que arreglar el colapso de errores de
  `_check_in_sacs`, que hoy no distingue "cédula no registrada" de "SACS caído".
- **Buscar en SACS por matrícula.** Habilitaría verificar a los 1621 con licencia numérica, pero es
  una integración nueva a investigar: el cliente actual solo hace `getPrfsnalByCed`.

## Open Questions

Las tres del spec, y todas son tuyas: el **texto** del correo, si se anuncia **fecha límite**, y el
**remitente**. Ninguna bloquea T1-T5; todas bloquean el envío.
