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
from src.models.patient import Patient
from src.models.specialty import Specialty
from src.services.specialties import can_attend_consultation, matches_consultation

# Ventana de presencia: el paciente está "presente" si su heartbeat es < 5 min
# (igual que PRESENCE_WINDOW_MS en panel-medico.tsx).
PRESENCE_WINDOW = timedelta(minutes=5)


async def list_queue(session: AsyncSession, limit: int = 100) -> list[Consultation]:
    stmt = (
        select(Consultation)
        .where(Consultation.status == "waiting")
        .order_by(Consultation.queued_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _is_present(consultation: Consultation, now: datetime) -> bool:
    last = consultation.patient_last_seen_at
    return last is not None and (now - last) < PRESENCE_WINDOW


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


async def attend_next(
    session: AsyncSession,
    assigned_doctor_id: uuid.UUID,
    specialty: str | None = None,
    is_admin: bool = False,
) -> Consultation:
    """Selecciona y toma el siguiente paciente (réplica de attendNext del frontend):

    1. Elegibles por `can_attend_consultation` (admin atiende todo). El matching es por la
       ESPECIALIDAD solicitada (consultations.specialty_id — el registro del paciente siempre
       la setea); category/needs quedan de fallback para consultas viejas sin especialidad.
    2. Preferir presentes (heartbeat < 5 min); si ninguno, caer a todos los elegibles.
    3. Preferir match de especialidad; si no, el más antiguo (FIFO).
    4. Toma atómica de ese caso (with_for_update nowait).
    """
    # Candidatos en espera + needs_tags del paciente + nombre de la especialidad, FIFO.
    stmt = (
        select(Consultation, Patient.needs_tags, Specialty.name)
        .join(Patient, Consultation.patient_id == Patient.id)
        .outerjoin(Specialty, Consultation.specialty_id == Specialty.id)
        .where(Consultation.status == "waiting")
        .order_by(Consultation.queued_at.asc())
    )
    rows = (await session.execute(stmt)).all()

    eligible = [
        (c, needs, c_spec)
        for (c, needs, c_spec) in rows
        if is_admin or can_attend_consultation(specialty, c_spec, c.category, needs)
    ]
    if not eligible:
        raise NotFoundError("No hay pacientes para tu especialidad ahora.")

    now = datetime.now(UTC)
    present = [row for row in eligible if _is_present(row[0], now)]
    pool = present if present else eligible

    if is_admin:
        chosen = pool[0][0]
    else:
        chosen = next(
            (
                c
                for (c, needs, c_spec) in pool
                if matches_consultation(specialty, c_spec, c.category, needs)
            ),
            pool[0][0],
        )

    # Toma atómica del elegido (otro médico pudo habérselo llevado en el ínterin).
    locked = await _lock_waiting(session, chosen.id)
    if locked is None:
        raise NotFoundError("El turno ya no está disponible.")
    return await _assign(session, locked, assigned_doctor_id)


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
