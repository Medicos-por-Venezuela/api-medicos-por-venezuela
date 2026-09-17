"""Sala de espera del paciente (R5 de tasks/cola-por-especialidad/spec.md).

El paciente solo debe ver "Entrar a la videoconsulta" cuando un médico tomó su caso: antes de
esto la sala se le mostraba desde el registro y entraba a una videollamada vacía. Este módulo
responde "¿en qué punto está mi caso?" para `/sala-espera` y `/mi-caso`, por JSON y por SSE.

Sigue la cadena hacia abajo: si al paciente lo derivaron, su caso vigente es la consulta hija
(la que está en la cola del especialista), aunque él solo conozca el id y el token del original.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import consultation_token
from src.core.errors import NotFoundError
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.schemas.consultation import WaitingRoomResponse

logger = logging.getLogger("mpv.api")

PHASE_WAITING = "waiting"  # en cola, sin médico
PHASE_READY = "ready"  # un médico tomó el caso y la sala existe
PHASE_SCHEDULED = "scheduled"  # cita agendada (módulo Agenda)
PHASE_FINISHED = "finished"  # cerrado, ausente, cancelado, derivado sin hija...

_ATTENDING_STATUSES = ("waiting", "in_progress", "contacted_whatsapp")


async def current_in_chain(session: AsyncSession, consultation_id: uuid.UUID) -> Consultation:
    """El caso vigente del paciente: baja por la cadena siguiendo siempre a la hija más reciente.

    Hacia abajo y nunca hacia arriba ni a los lados: quien tiene acceso a una consulta lo tiene
    a lo que se derivó de ella (mismo paciente), no a lo que vino antes."""
    current = await session.get(Consultation, consultation_id)
    if current is None:
        raise NotFoundError("Consulta no encontrada.")
    seen = {current.id}
    while True:
        child = await session.scalar(
            select(Consultation)
            .where(Consultation.parent_consultation_id == current.id)
            .order_by(Consultation.created_at.desc(), Consultation.id.desc())
            .limit(1)
        )
        if child is None or child.id in seen:
            return current
        seen.add(child.id)
        current = child


def phase_of(consultation: Consultation) -> str:
    if consultation.status == "scheduled":
        return PHASE_SCHEDULED
    if consultation.status not in _ATTENDING_STATUSES:
        return PHASE_FINISHED
    if consultation.assigned_doctor_id is not None and consultation.video_room_url:
        return PHASE_READY
    # En cola, o tomado sin sala todavía (un caso legacy cuyo médico aún no la abrió): para el
    # paciente las dos cosas son "espera", y no debe ver un botón que lo lleve a ninguna parte.
    return PHASE_WAITING


async def snapshot(session: AsyncSession, consultation_id: uuid.UUID) -> WaitingRoomResponse:
    """Estado de la sala para el paciente, sin token (ver `with_access_token`)."""
    current = await current_in_chain(session, consultation_id)
    phase = phase_of(current)

    async def specialty_name(specialty_id: uuid.UUID | None) -> str | None:
        if specialty_id is None:
            return None
        return await session.scalar(select(Specialty.name).where(Specialty.id == specialty_id))

    doctor_name = None
    if phase == PHASE_READY:
        doctor_name = await session.scalar(
            select(Profile.full_name).where(Profile.id == current.assigned_doctor_id)
        )
    patient_name = await session.scalar(
        select(Patient.full_name).where(Patient.id == current.patient_id)
    )
    return WaitingRoomResponse(
        consultation_id=current.id,
        code=current.code,
        status=current.status,
        phase=phase,
        specialty=await specialty_name(current.specialty_id),
        derived_from_specialty=await specialty_name(current.derived_from_specialty_id),
        doctor_name=doctor_name,
        # La sala solo cuando hay alguien dentro: es justo lo que no se le debe dar antes.
        video_room_url=current.video_room_url if phase == PHASE_READY else None,
        scheduled_at=current.scheduled_at,
        patient_first_name=(patient_name or "").split()[0] if patient_name else None,
    )


def with_access_token(state: WaitingRoomResponse, requested_id: uuid.UUID) -> WaitingRoomResponse:
    """Si el caso vigente no es el que se pidió (lo derivaron), adjunta un token para el nuevo:
    sin él, el paciente que entró con el token del original no podría marcar que entró a la
    videollamada del especialista."""
    if state.consultation_id == requested_id:
        return state
    return state.model_copy(
        update={"access_token": consultation_token.issue(state.consultation_id)}
    )


def _event(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def sse_events(
    fetch: Callable[[], Awaitable[WaitingRoomResponse]],
    *,
    requested_id: uuid.UUID,
    poll_seconds: float,
    heartbeat_seconds: float,
    max_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[str]:
    """Stream SSE del estado de la sala: un evento `status` al conectar y en cada cambio.

    - Latido (`: ping`) si pasa `heartbeat_seconds` sin eventos, para que ningún proxy corte la
      conexión por inactividad.
    - Termina cuando el caso se da por terminado o a los `max_seconds`: el cliente reconecta. Un
      stream nunca vive indefinidamente.
    - Si el caso desaparece, emite `gone` y termina; un fallo de base se registra y termina (el
      cliente cae a pedir el JSON).

    `fetch` abre y cierra su propia sesión por ciclo: el stream no retiene una conexión del pool
    mientras el paciente espera."""
    started = clock()
    last_state: WaitingRoomResponse | None = None
    last_emit = started
    yield "retry: 5000\n\n"
    while True:
        try:
            state = await fetch()
        except NotFoundError:
            yield _event("gone", {})
            return
        except Exception:  # noqa: BLE001 — el stream termina y el cliente cae al JSON
            logger.warning(
                "SSE:waiting_room_error consultation_id=%s", requested_id, exc_info=True
            )
            return
        now = clock()
        if state != last_state:
            yield _event("status", with_access_token(state, requested_id).model_dump(mode="json"))
            last_state = state
            last_emit = now
            if state.phase == PHASE_FINISHED:
                return
        elif now - last_emit >= heartbeat_seconds:
            yield ": ping\n\n"
            last_emit = now
        if now - started >= max_seconds:
            return
        await sleep(poll_seconds)
