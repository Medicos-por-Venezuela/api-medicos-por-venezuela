"""Capa de negocio de la cola (Board) con bloqueo pesimista de fallo rápido.

NUNCA hace un select + update común: usa with_for_update(nowait=True) para que,
si la fila ya está bloqueada por otra transacción, PostgreSQL lance el error de lock
de inmediato (el router lo traduce a 409 sin que la petición se cuelgue).
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError
from src.models.consultation import Consultation

# La ventana de presencia y `_is_present` se eliminaron con `attend_next`: leían
# `patient_last_seen_at`, columna que dejó de escribirse cuando la presencia del paciente pasó
# a Realtime Presence (lib/patientPresence.tsx). El panel muestra quién está en sala con el
# badge "● En sala", que sale de ese canal y no de la base.


async def list_queue(session: AsyncSession, limit: int = 100) -> list[Consultation]:
    stmt = (
        select(Consultation)
        .where(Consultation.status == "waiting")
        .order_by(Consultation.queued_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _lock_waiting(session: AsyncSession, consultation_id: uuid.UUID) -> Consultation | None:
    """Bloquea la fila SI sigue 'waiting' (fallo rápido). None si ya no está disponible."""
    stmt = (
        select(Consultation)
        .where(Consultation.id == consultation_id, Consultation.status == "waiting")
        .with_for_update(nowait=True)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _assign(
    session: AsyncSession, consultation: Consultation, assigned_doctor_id: uuid.UUID
) -> Consultation:
    now = datetime.now(UTC)
    consultation.status = "in_progress"
    consultation.assigned_doctor_id = assigned_doctor_id
    consultation.opened_at = consultation.opened_at or now
    consultation.started_at = consultation.started_at or now
    await session.commit()
    await session.refresh(consultation)
    return consultation


async def take_consultation(
    session: AsyncSession, consultation_id: uuid.UUID, assigned_doctor_id: uuid.UUID
) -> Consultation:
    """Bloquea y asigna una consulta concreta. Lanza el error de lock si la fila
    está bloqueada por otra transacción; NotFoundError si ya no está disponible."""
    consultation = await _lock_waiting(session, consultation_id)
    if consultation is None:
        raise NotFoundError("El turno ya no está disponible.")
    return await _assign(session, consultation, assigned_doctor_id)


# `attend_next` se eliminó: ningún cliente lo llamaba (el panel selecciona en el cliente y
# llama a POST /consultations/{id}/claim), y su preferencia por "paciente presente" dependía de
# `patient_last_seen_at`, que ya no escribe nadie desde que la presencia pasó a Realtime
# Presence. La regla de especialidad que era su única parte viva vive ahora en
# `consultations.claim_consultation` y en `get_panel`, que sí están en uso.


async def release_stale(session: AsyncSession, older_than_minutes: int) -> int:
    """Resiliencia: devuelve a 'waiting' las consultas 'in_progress' abiertas hace más
    de `older_than_minutes` (un médico abrió el caso y no lo cerró). Las libera para que
    otro médico pueda tomarlas. Devuelve cuántas se liberaron. Pensado para un CRON/worker."""
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    stmt = (
        update(Consultation)
        .where(Consultation.status == "in_progress", Consultation.opened_at < cutoff)
        .values(status="waiting", assigned_doctor_id=None, opened_at=None, started_at=None)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount or 0
