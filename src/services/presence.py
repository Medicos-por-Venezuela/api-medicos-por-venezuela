"""Presencia del paciente en un hilo: dos vías, una sola pregunta.

Módulo propio y no una sección de `messaging` porque lo necesitan los **dos lados** del aviso:
`messaging` la REGISTRA y `notifications` la CONSULTA para decidir si difiere el correo. Con la
presencia dentro de `messaging`, esa consulta cerraba un ciclo de imports (`messaging` ya importa
`notifications`), y el import local que lo esquivaba escondía la dependencia en vez de resolverla.

Dos vías, a propósito:

- **En memoria del proceso** (`_ACTIVE_PATIENTS`): la vía rápida, sin IO. Es la que aguanta que
  el SSE de la sala pregunte cada `WAITING_ROOM_POLL_SECONDS`. Se pierde al reiniciar y no la
  ven las otras réplicas.
- **`consultations.patient_last_seen_at`**: la vía compartida. Sobrevive al reinicio y la ven
  todas las réplicas, pero es un UPDATE sobre una tabla con CDC hacia el Board — por eso va
  throttleada (`MESSAGING_PRESENCE_PERSIST_SECONDS`).

Hasta 2026-10-16 la columna solo la escribían los mensajes y adjuntos del paciente, así que la
señal MÁS COMÚN —estar en la sala de espera con el stream abierto— no dejaba rastro compartido:
tras un reinicio, el buzón del médico decía «Desconectado» de quien estaba delante de la
pantalla. `touch_patient_presence` existe para que no haya forma de registrar una señal y
olvidarse de una de las dos vías.

**Asimetría (CA4.3/CA7.2 de `tasks/mensajeria-medico-paciente/spec.md`):** aquí solo se modela la
presencia del PACIENTE, que ve su médico tratante. La del médico no se registra ni se expone al
paciente, y no es un olvido.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.consultation import Consultation

# Registro en vivo: consulta -> última señal. Ver el docstring del módulo para por qué conviven
# con la columna y qué garantiza cada una.
_ACTIVE_PATIENTS: dict[uuid.UUID, datetime] = {}


def _aware(when: datetime | None) -> datetime | None:
    """Normaliza a UTC consciente. Las columnas son `timestamptz`, pero un doble de test o una
    fila vieja pueden traer un naive, y comparar naive con aware lanza `TypeError` justo en el
    camino que decide si se manda un correo."""
    if when is None:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


def record_patient_presence(consultation_id: uuid.UUID) -> None:
    """Registra actividad reciente del paciente SOLO en memoria (vía rápida).

    Para una señal completa usa `touch_patient_presence`: esta se queda corta en cuanto hay más
    de un proceso, y es justo el error que el módulo vino a corregir."""
    _ACTIVE_PATIENTS[consultation_id] = datetime.now(UTC)


def is_patient_online(consultation_id: uuid.UUID, last_seen_at: datetime | None = None) -> bool:
    """¿El paciente dio señales dentro de `MESSAGING_PATIENT_PRESENCE_TTL_SECONDS`?

    Memoria primero (más fresca), columna después (compartida). Basta una de las dos: la columna
    va throttleada y puede ir hasta 30 s por detrás de la memoria, así que preguntarle solo a
    ella daría «Desconectado» de alguien que acaba de escribir.
    """
    now = datetime.now(UTC)
    ttl = settings.MESSAGING_PATIENT_PRESENCE_TTL_SECONDS

    active_ts = _ACTIVE_PATIENTS.get(consultation_id)
    if active_ts:
        if (now - active_ts).total_seconds() <= ttl:
            return True
        _ACTIVE_PATIENTS.pop(consultation_id, None)

    persisted = _aware(last_seen_at)
    return persisted is not None and (now - persisted).total_seconds() <= ttl


def touch_patient_presence(consultation: Consultation) -> bool:
    """Registra una señal del paciente en las DOS vías. True si tocó la columna.

    No recibe sesión ni commitea: muta la fila ya cargada y deja que la transacción del caller
    la persista, que es como el resto de `messaging` escribe. El throttle se decide con el valor
    que la fila ya trae, así que no cuesta ninguna consulta extra.

    El throttle NO es una optimización prematura: sin él, el stream de la sala escribiría esta
    fila cada 4 s por paciente en espera, sobre una tabla con CDC hacia el Board. Con el TTL de
    «en línea» en 45 s y el throttle en 30, ningún veredicto cambia por esperar.
    """
    record_patient_presence(consultation.id)
    now = datetime.now(UTC)
    last = _aware(consultation.patient_last_seen_at)
    if (
        last is not None
        and (now - last).total_seconds() < settings.MESSAGING_PRESENCE_PERSIST_SECONDS
    ):
        return False
    consultation.patient_last_seen_at = now
    return True


async def persist_patient_presence(session: AsyncSession, consultation_id: uuid.UUID) -> bool:
    """Señal de presencia para los llamantes que NO tienen la consulta cargada ni transacción
    propia — hoy solo el stream SSE de la sala de espera. True si escribió.

    Commitea ella misma, y solo cuando el throttle deja pasar la escritura: el stream no tiene
    una unidad de trabajo que cerrar, y un commit por vuelta sería un commit vacío cada 4 s.
    Devuelve False en silencio si la consulta no existe: el stream ya tiene su propio camino de
    `gone`, y la presencia no es el sitio donde descubrirlo.
    """
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        return False
    if not touch_patient_presence(consultation):
        return False
    await session.commit()
    return True
