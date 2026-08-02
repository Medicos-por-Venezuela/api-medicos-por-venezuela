"""Lógica de Interconsultas: segunda opinión EN TIEMPO REAL durante una consulta activa.

Ver .knowledge/interconsultas.md. La consulta sigue ABIERTA. El médico que atiende invita a UN
médico del pool; ambos comparten el video. El invitado ve datos LIMITADOS (motivo, notas, edad).
No confundir con "Agendar con Especialista" (que cierra la consulta y agenda para otro día).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, ForbiddenError, NotFoundError
from src.models.consultation import Consultation
from src.models.interconsultation import Interconsultation
from src.models.patient import Patient
from src.models.profile import Profile
from src.schemas.interconsultation import InterconsultationForInvitee, InterconsultationResponse
from src.services import audit


async def _invited_name(session: AsyncSession, invited_doctor_id: uuid.UUID) -> str | None:
    """Nombre del médico invitado (colega, NO el paciente): para la UI del que atiende."""
    return await session.scalar(select(Profile.full_name).where(Profile.id == invited_doctor_id))


def _to_response(inter: Interconsultation, invited_name: str | None) -> InterconsultationResponse:
    return InterconsultationResponse(
        id=inter.id,
        consultation_id=inter.consultation_id,
        invited_doctor_id=inter.invited_doctor_id,
        invited_doctor_name=invited_name,
        created_by_id=inter.created_by_id,
        status=inter.status,
        note=inter.note,
        created_at=inter.created_at,
    )


async def create_interconsultation(
    session: AsyncSession,
    *,
    consultation_id: uuid.UUID,
    invited_doctor_id: uuid.UUID,
    created_by_id: uuid.UUID,
    note: str | None = None,
) -> InterconsultationResponse:
    """El médico que ATIENDE invita a UN médico del pool. La consulta sigue abierta.

    Guardas: la consulta existe; quien invita es el médico asignado; no se invita a sí mismo;
    1 interconsulta por consulta (por ahora).
    """
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")
    if consultation.assigned_doctor_id != created_by_id:
        raise ForbiddenError(
            "Solo el médico que atiende la consulta puede asignar una interconsulta."
        )
    if invited_doctor_id == created_by_id:
        raise ConflictError("No puedes asignarte la interconsulta a ti mismo.")
    existing = await session.scalar(
        select(Interconsultation).where(Interconsultation.consultation_id == consultation_id)
    )
    if existing is not None:
        raise ConflictError("Esta consulta ya tiene una interconsulta asignada.")

    inter = Interconsultation(
        consultation_id=consultation_id,
        invited_doctor_id=invited_doctor_id,
        created_by_id=created_by_id,
        note=note,
    )
    session.add(inter)
    await session.flush()

    # Historial (MVP): quién invitó a quién, cuándo.
    await audit.log_action(
        session,
        action="interconsultation.created",
        actor_user_id=created_by_id,
        resource="interconsultations",
        resource_id=inter.id,
        metadata={
            "consultation_id": str(consultation_id),
            "invited_doctor_id": str(invited_doctor_id),
        },
    )

    # SIN ESTE COMMIT la interconsulta NO se guarda. `get_db` cierra la sesión al terminar el
    # request y eso hace ROLLBACK: el `flush()` de arriba manda el INSERT y rellena `inter.id`,
    # así que la API respondía 201 con un id de verdad mientras la fila se descartaba. Se detectó
    # en producción con `select count(*) from interconsultations` = 0 y respuestas 201 correctas.
    await session.commit()
    await session.refresh(inter)

    return _to_response(inter, await _invited_name(session, invited_doctor_id))


async def get_for_consultation(
    session: AsyncSession, consultation_id: uuid.UUID
) -> InterconsultationResponse | None:
    """La interconsulta de una consulta (para el médico que atiende). None si no hay."""
    inter = await session.scalar(
        select(Interconsultation).where(Interconsultation.consultation_id == consultation_id)
    )
    if inter is None:
        return None
    return _to_response(inter, await _invited_name(session, inter.invited_doctor_id))


async def list_for_invitee(
    session: AsyncSession, invited_doctor_id: uuid.UUID
) -> list[InterconsultationForInvitee]:
    """Interconsultas asignadas a un médico invitado, con datos LIMITADOS (motivo, notas, edad y
    el video para unirse) — SIN identidad del paciente."""
    stmt = (
        select(
            Interconsultation.id,
            Interconsultation.consultation_id,
            Interconsultation.status,
            Interconsultation.note,
            Interconsultation.created_at,
            Consultation.chief_complaint,
            Consultation.internal_note,
            Consultation.clinical_notes,
            Consultation.video_room_url,
            Patient.age_range.label("patient_age_range"),
        )
        .join(Consultation, Interconsultation.consultation_id == Consultation.id)
        .outerjoin(Patient, Consultation.patient_id == Patient.id)
        .where(Interconsultation.invited_doctor_id == invited_doctor_id)
        .order_by(Interconsultation.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        InterconsultationForInvitee(
            id=r.id,
            consultation_id=r.consultation_id,
            status=r.status,
            note=r.note,
            chief_complaint=r.chief_complaint,
            internal_note=r.internal_note,
            clinical_notes=r.clinical_notes,
            patient_age_range=r.patient_age_range,
            video_room_url=r.video_room_url,
            created_at=r.created_at,
        )
        for r in rows
    ]
