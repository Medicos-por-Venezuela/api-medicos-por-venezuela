"""Capa de negocio para consultations y sus eventos."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableError,
)
from src.models.consultation import CONSULTATION_STATUSES, Consultation
from src.models.consultation_event import ConsultationEvent
from src.models.patient import Patient
from src.models.specialty import Specialty
from src.schemas.consultation import ConsultationCreate, ConsultationUpdate
from src.schemas.consultation_event import ConsultationEventCreate
from src.services.jitsi import new_room_url
from src.services.specialties import compute_priority

# Estados en los que la consulta sigue "viva" para el heartbeat del paciente.
_HEARTBEAT_OPEN_STATUSES = {"waiting", "in_progress"}
# Resultados de cierre permitidos.
_CLOSE_OUTCOMES = {"closed", "patient_no_show"}


def _validate_status(value: str | None) -> None:
    if value is not None and value not in CONSULTATION_STATUSES:
        raise UnprocessableError(f"Estado inválido. Permitidos: {sorted(CONSULTATION_STATUSES)}")


async def list_consultations(
    session: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    patient_id: uuid.UUID | None = None,
    viewer_is_staff: bool = True,
    viewer_user_id: uuid.UUID | None = None,
) -> list[Consultation]:
    _validate_status(status)
    stmt = select(Consultation)
    if status:
        stmt = stmt.where(Consultation.status == status)
    if patient_id:
        stmt = stmt.where(Consultation.patient_id == patient_id)
    if not viewer_is_staff:
        # Un paciente solo ve las consultas ligadas a su propia cuenta (RLS select_own).
        stmt = stmt.join(Patient, Consultation.patient_id == Patient.id).where(
            Patient.user_id == viewer_user_id
        )
    stmt = stmt.order_by(Consultation.created_at.desc()).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_consultation(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    viewer_is_staff: bool = True,
    viewer_user_id: uuid.UUID | None = None,
) -> Consultation:
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")
    if not viewer_is_staff:
        # Verificación de pertenencia (anti-IDOR): la consulta debe ser del paciente.
        patient = await session.get(Patient, consultation.patient_id)
        if patient is None or patient.user_id != viewer_user_id:
            raise NotFoundError("Consulta no encontrada.")
    return consultation


async def create_consultation(session: AsyncSession, data: ConsultationCreate) -> Consultation:
    _validate_status(data.status)
    patient = await session.get(Patient, data.patient_id)
    if patient is None:
        raise BadRequestError("El paciente referenciado (patient_id) no existe.")
    if data.specialty_id is not None and await session.get(Specialty, data.specialty_id) is None:
        raise BadRequestError("La especialidad referenciada (specialty_id) no existe.")
    # code lo asigna SIEMPRE el trigger generate_consultation_code en la base.
    consultation = Consultation(**data.model_dump())

    # Derivación de campos desde las necesidades del paciente (igual que el registro
    # del frontend), solo cuando no vienen explícitos.
    needs = patient.needs_tags or []
    if consultation.category is None and needs:
        consultation.category = needs[0]
    if consultation.chief_complaint is None:
        consultation.chief_complaint = patient.description or (", ".join(needs) or None)
    if data.priority == "normal":
        consultation.priority = compute_priority(needs)

    session.add(consultation)
    await session.commit()
    await session.refresh(consultation)
    return consultation


async def close_consultation(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    outcome: str,
    closed_by: uuid.UUID | None = None,
    note: str | None = None,
) -> Consultation:
    """Cierra una consulta (`closed`) o la marca como ausencia (`patient_no_show`),
    guardando la nota y registrando el evento de auditoría (réplica de closeConsultation)."""
    if outcome not in _CLOSE_OUTCOMES:
        raise UnprocessableError(f"Resultado inválido. Permitidos: {sorted(_CLOSE_OUTCOMES)}")
    consultation = await get_consultation(session, consultation_id)
    consultation.status = outcome
    consultation.closed_at = datetime.now(UTC)
    if note is not None:
        consultation.internal_note = note

    event = ConsultationEvent(
        consultation_id=consultation_id,
        event_type=outcome,
        created_by=closed_by,
        note=note,
    )
    session.add(event)
    await session.commit()
    await session.refresh(consultation)
    return consultation


async def heartbeat(session: AsyncSession, consultation_id: uuid.UUID) -> Consultation:
    """Marca presencia del paciente (mark_patient_waiting): actualiza
    patient_last_seen_at solo si la consulta sigue en espera o en progreso."""
    consultation = await get_consultation(session, consultation_id)
    if consultation.status in _HEARTBEAT_OPEN_STATUSES:
        consultation.patient_last_seen_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(consultation)
    return consultation


async def ensure_video_room(session: AsyncSession, consultation_id: uuid.UUID) -> Consultation:
    """Genera (idempotente) la sala Jitsi de la consulta. Si ya existe, la devuelve;
    solo crea una nueva si la consulta está en espera (réplica de /api/videoconsulta)."""
    consultation = await get_consultation(session, consultation_id)
    if consultation.video_room_url:
        return consultation
    if consultation.status != "waiting":
        raise ConflictError("La consulta no está en espera.")
    consultation.video_room_url = new_room_url()
    await session.commit()
    await session.refresh(consultation)
    return consultation


async def update_consultation(
    session: AsyncSession, consultation_id: uuid.UUID, data: ConsultationUpdate
) -> Consultation:
    _validate_status(data.status)
    consultation = await get_consultation(session, consultation_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(consultation, field, value)
    await session.commit()
    await session.refresh(consultation)
    return consultation


async def delete_consultation(session: AsyncSession, consultation_id: uuid.UUID) -> None:
    consultation = await get_consultation(session, consultation_id)
    await session.delete(consultation)
    await session.commit()


# --- Eventos / auditoría ---


async def list_events(
    session: AsyncSession, consultation_id: uuid.UUID
) -> list[ConsultationEvent]:
    await get_consultation(session, consultation_id)  # 404 si no existe
    stmt = (
        select(ConsultationEvent)
        .where(ConsultationEvent.consultation_id == consultation_id)
        .order_by(ConsultationEvent.created_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_event(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    data: ConsultationEventCreate,
    created_by: uuid.UUID | None = None,
) -> ConsultationEvent:
    await get_consultation(session, consultation_id)  # 404 si no existe
    if data.consultation_id != consultation_id:
        raise BadRequestError("El consultation_id del cuerpo no coincide con el de la ruta.")
    # created_by SIEMPRE del JWT (no del body) — anti-IDOR.
    event = ConsultationEvent(
        consultation_id=data.consultation_id,
        event_type=data.event_type,
        note=data.note,
        created_by=created_by,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event
