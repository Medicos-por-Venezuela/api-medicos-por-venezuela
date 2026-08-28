# Spec: comando de campaña para pedir la cédula a los médicos antiguos

> Plan: [`plan.md`](./plan.md) · Tareas: [`todo.md`](./todo.md)
>
> Todas las cifras salen de la BD local, que es un **restore de producción** anterior al
> 2026-08-27. Prod tendrá unas decenas más; los órdenes de magnitud no cambian. El comando debe
> recalcularlas en vivo, nunca asumirlas.

## Objective

Que los **2846 médicos registrados antes del 2026-07-14 que no tienen cédula** reciban un correo
pidiéndoles que la completen en `/panel-medico/perfil`, donde ya se valida contra SACS/FPV al
guardar.

## Por qué un correo y no código

El mecanismo para pedir la cédula **ya existe y funciona**. `pages/panel-medico.tsx` redirige a
`/panel-medico/perfil` a cualquier médico sin cédula, y está en producción desde el 2026-07-14.

La prueba de que funciona: de las 109 fichas con cédula, **89 son cuentas antiguas que la
completaron después de que ese redirect saliera**. Cuando el médico entra, el flujo hace su trabajo.

El problema es de **alcance**, no de lógica: el redirect solo dispara si el médico entra al panel.

| | |
| --- | --- |
| Médicos antiguos sin cédula | **2846** |
| Completaron en 6 semanas | 89 |
| Conversión | **~3%** |

Se registraron durante la emergencia y no han vuelto. No hay bug que arreglar; hay gente a la que
llegar por otro canal.

## Lo que NO es este cambio

**No es el barrido de SACS que se pidió originalmente.** Se investigó y no procede todavía:

- El cliente de SACS busca con `getPrfsnalByCed` — **por cédula**. Solo 109 de 2956 fichas tienen
  una. Para las otras 2847 no hay nada que consultar.
- De esas 109, ya pasaron todas por `_verify_credential` al guardarse (74 verificadas, 35 no), así
  que un barrido re-confirmaría lo ya sabido. Tiene sentido como job periódico —detectar
  credenciales caducadas— pero no es urgente.
- **`_check_in_sacs` descarta el campo `error`** de `verificar_sacs` y colapsa cinco desenlaces
  distintos en un único "no verificado": cédula no registrada, error HTTP, error de conexión,
  respuesta inesperada y formato inválido. Un barrido masivo durante una caída de SACS marcaría a
  **todos** como no verificados y, desde el PR #72, eso les impide atender. Ese colapso hay que
  arreglarlo **antes** de cualquier barrido que escriba en `verified`.

## Qué se construye

Un comando de artisan:

```
python artisan doctors:pedir-cedula [--segmento X] [--limite N] [--enviar]
```

**Sin `--enviar` no manda nada.** El modo por defecto imprime a quién escribiría y por qué. El flag
va en el lado peligroso a propósito: un comando que envía correos a 2846 personas no debe poder
dispararse por un `Enter` de más.

### Segmentación

De los 2846, sus licencias escritas a mano se reparten así:

| Segmento | Cuántos | Por qué importa |
| --- | --- | --- |
| `sin-licencia` | **762** | Marcados como verificados sin **ningún** dato verificable. Los más urgentes |
| `licencia-numerica` | 1621 | Solo dígitos: parece una matrícula real, pero nadie la contrastó |
| `texto-libre` | 464 | Cosas como *"Universidad Nacional De Chimborazo"*: ni siquiera es una matrícula |

### A quién NO se le escribe

El comando excluye, y lo dice en el resumen:

- Quien ya tiene cédula (se comprueba **en el momento del envío**, no contra una lista previa: entre
  el dry-run y el envío puede haberla completado).
- Cuentas con `active = false` — revocadas por un admin.
- Fichas con `deleted_at` no nulo.
- Sin email.
- **Quien tenga desactivado el aviso en sus preferencias** (ver abajo).
- Quien ya recibió este correo hace menos de `REENVIO_DIAS` días.

### Respeta las preferencias existentes

Ya hay un sistema de preferencias (`users.notification_prefs` + `should_notify()` en
`services/notifications.py`). La campaña **se integra en él**, no lo esquiva: se añade el evento
`credential_reminder` con canal `email` a `NOTIFICATION_EVENTS`, y el comando pasa por
`should_notify` como cualquier otro aviso. Un médico que ya dijo que no quiere correos no recibe
este tampoco.

### Registro de envíos

Tabla nueva `credential_reminders` (`user_id`, `sent_at`, `segment`, `batch_id`), con índice único
por `user_id` + día. Sin ella no se puede:

- Reenviar **solo** a quien no completó.
- Medir la conversión de verdad (hoy solo se puede inferir).
- Garantizar que un fallo a mitad del lote no duplique correos al reintentar.

## Tech Stack

- **Backend** `api-medicos-por-venezuela`: FastAPI + SQLAlchemy async + `artisan` (CLI propio).
- **Correo**: Mailtrap vía `services/mail.py`. `send_mail` es *best-effort* y devuelve `bool`.
- **Sandbox**: con `MAILTRAP_INBOX_ID` configurado, el SDK entrega a un inbox de prueba en vez de a
  destinatarios reales. Es el ensayo general antes del envío de verdad.

## Commands

```
# 1. Ver a quién se escribiría (no envía nada)
python artisan doctors:pedir-cedula

# 2. Solo el segmento más urgente
python artisan doctors:pedir-cedula --segmento sin-licencia

# 3. Ensayo real contra el inbox de Mailtrap (con MAILTRAP_INBOX_ID puesto)
python artisan doctors:pedir-cedula --segmento sin-licencia --limite 5 --enviar

# 4. Envío real, por lotes
python artisan doctors:pedir-cedula --segmento sin-licencia --limite 100 --enviar
```

Verificación: `pytest`, `ruff format --check`, `ruff check`.

## Code Style

El punto crítico del comando: revalidar en el momento del envío.

```python
# El dry-run y el envío pueden estar separados por horas. Se relee la ficha justo antes de
# escribir: si el médico completó su cédula mientras tanto, no se le molesta.
if doctor.cedula and doctor.cedula.strip():
    skipped["ya_completada"] += 1
    continue
if not notifications.should_notify("credential_reminder", "email", prefs):
    skipped["opt_out"] += 1
    continue
```

Y el registro, dentro de la misma transacción que el envío se considera hecho:

```python
# `send_mail` es best-effort y devuelve False si Mailtrap lo rechaza. Solo se registra el envío
# cuando lo aceptó: si no, este médico entra en el siguiente lote en vez de perderse.
if await mail.send_mail(...):
    session.add(CredentialReminder(user_id=..., segment=..., batch_id=...))
```

## Testing Strategy

| Nivel | Qué cubre |
| --- | --- |
| pytest | Que sin `--enviar` **no se llama a `send_mail` ni una vez** (el test más importante) |
| pytest | Las cinco exclusiones: ya tiene cédula, inactivo, borrado, sin email, opt-out |
| pytest | Idempotencia: dos corridas seguidas no escriben dos veces al mismo |
| pytest | `--limite` respeta el tope y `--segmento` filtra bien |
| pytest | Si `send_mail` devuelve False, NO se registra el envío |
| Manual | Ensayo contra el inbox de Mailtrap antes de tocar destinatarios reales |

## Boundaries

**Always**

- Dry-run por defecto; enviar exige `--enviar` explícito.
- Revalidar la ficha en el momento del envío, no desde una lista previa.
- Pasar por `should_notify` como cualquier otro aviso del sistema.
- Registrar solo los envíos que Mailtrap aceptó.
- Espaciar los envíos: son ~2800 correos desde una IP; una ráfaga daña la reputación del dominio.

**Ask first**

- El primer envío real a destinatarios reales. **Siempre.** No es una acción reversible.
- Cambiar el texto del correo (es la voz del proyecto hacia sus médicos).
- Subir `--limite` por encima del primer lote de prueba.

**Never**

- Tocar `doctors.verified` desde este comando. Degradar el acceso es otra decisión, posterior a la
  campaña y con fecha límite anunciada.
- Escribir a quien ya dijo que no quiere correos.
- Meter en el correo datos de terceros o cualquier PII más allá del nombre del propio destinatario.
- Ejecutarlo sin `--limite` la primera vez.

## Success Criteria

1. Sin `--enviar`, el comando imprime el desglose por segmento y **no envía nada** (verificado por
   test, no por inspección).
2. Con `--limite 5 --enviar` y `MAILTRAP_INBOX_ID` puesto, llegan 5 correos al inbox de prueba y se
   registran 5 filas en `credential_reminders`.
3. Correrlo dos veces seguidas no escribe dos veces al mismo médico.
4. Un médico que completa su cédula entre el dry-run y el envío no recibe el correo.
5. Un médico con el aviso desactivado no lo recibe, y aparece contabilizado como `opt_out`.
6. Si `send_mail` falla, ese médico **no** queda registrado y entra en el siguiente lote.
7. `doctors.verified` no cambia en ninguna fila. Comprobable con un conteo antes y después.
8. El correo enlaza a `/panel-medico/perfil` y explica por qué se pide la cédula.
9. `pytest` verde; `ruff` limpio.

## Open Questions

Tres, y las tres son tuyas, no técnicas:

1. **El texto del correo.** Puedo redactar un borrador, pero es la voz del proyecto hacia sus
   médicos voluntarios y merece tu revisión antes de salir.
2. **Si se anuncia una fecha límite.** Cambia el tono por completo: "ayúdanos a completar tu ficha"
   no es lo mismo que "a partir del X no podrás atender sin cédula verificada". Lo segundo solo
   tiene sentido si de verdad vais a degradar `verified` después.
3. **El remitente.** `MAIL_FROM_EMAIL` actual sirve para avisos transaccionales; 2800 correos casi
   idénticos desde ese dominio es otro perfil de envío. Conviene mirar la reputación antes.
