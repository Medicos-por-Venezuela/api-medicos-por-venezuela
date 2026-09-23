"""Lógica de Interconsultas: segunda opinión EN TIEMPO REAL durante una consulta activa.

Ver .knowledge/interconsultas.md. La consulta sigue ABIERTA. El médico que atiende invita a UN
médico del pool; ambos comparten el video. El invitado ve datos LIMITADOS (motivo, notas, edad).
No confundir con "Agendar con Especialista" (que cierra la consulta y agenda para otro día).

Contenido clínico (motivo, notas, nota de la invitación): cifrado en la BD y descifrado solo para
el equipo tratante de ESE caso — el médico asignado y el invitado (decisión 2026-09-23). El admin
ve la interconsulta con esos campos en null. Cada lectura concedida va al audit_log con
`resource = "consultations"` y el id de la consulta.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, ForbiddenError, NotFoundError
from src.core.security import Principal
from src.models.consultation import Consultation
from src.models.interconsultation import Interconsultation
from src.models.patient import Patient
from src.models.profile import Profile
from src.schemas.clinical import ClinicalGrant, clinical_context
from src.schemas.interconsultation import InterconsultationForInvitee, InterconsultationResponse
from src.services import audit, clinical_access

_RESOURCE = "consultations"


async def _invited_name(session: AsyncSession, invited_doctor_id: uuid.UUID) -> str | None:
    """Nombre del médico invitado (colega, NO el paciente): para la UI del que atiende."""
    return await session.scalar(select(Profile.full_name).where(Profile.id == invited_doctor_id))


def _to_response(
    inter: Interconsultation, invited_name: str | None, grant: ClinicalGrant | None
) -> InterconsultationResponse:
    return InterconsultationResponse.model_validate(
        {
            "id": inter.id,
            "consultation_id": inter.consultation_id,
            "invited_doctor_id": inter.invited_doctor_id,
            "invited_doctor_name": invited_name,
            "created_by_id": inter.created_by_id,
            "status": inter.status,
            "note": inter.note,
            "created_at": inter.created_at,
        },
        context=clinical_context(grant),
    )


async def create_interconsultation(
    session: AsyncSession,
    *,
    consultation_id: uuid.UUID,
    invited_doctor_id: uuid.UUID,
    principal: Principal,
    note: str | None = None,
    ip: str | None = None,
) -> InterconsultationResponse:
    """El médico que ATIENDE invita a UN médico del pool. La consulta sigue abierta.

    Guardas: la consulta existe; quien invita es el médico asignado; no se invita a sí mismo;
    1 interconsulta por consulta (por ahora).
    """
    created_by_id = principal.id
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

    # Quien invita es el asignado (comprobado arriba): su propia nota vuelve en claro si ejerce.
    grant = clinical_access.treating_doctor_grant(principal, consultation.assigned_doctor_id)
    response = _to_response(inter, await _invited_name(session, invited_doctor_id), grant)
    await clinical_access.audit_clinical_read(
        session, principal=principal, ip=ip, resource=_RESOURCE, grants=[(consultation_id, grant)]
    )
    return response


async def get_for_consultation(
    session: AsyncSession, consultation_id: uuid.UUID, *, principal: Principal, ip: str | None
) -> InterconsultationResponse | None:
    """La interconsulta de una consulta. None si no hay.

    Solo para el médico habilitado asignado a esa consulta (con la nota en claro) o un admin
    (la misma respuesta con la nota en null). Cualquier otro médico recibe 403 y el intento queda
    en el audit_log: antes bastaba ser staff para leer la nota de la interconsulta de cualquier
    caso conociendo su id."""
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")
    grant = clinical_access.treating_doctor_grant(principal, consultation.assigned_doctor_id)
    if grant is None and not principal.is_admin:
        await clinical_access.audit_clinical_denied(
            session, principal=principal, ip=ip, resource=_RESOURCE, resource_id=consultation_id
        )
        raise ForbiddenError("Solo el médico que atiende la consulta puede ver su interconsulta.")
    inter = await session.scalar(
        select(Interconsultation).where(Interconsultation.consultation_id == consultation_id)
    )
    if inter is None:
        return None
    response = _to_response(inter, await _invited_name(session, inter.invited_doctor_id), grant)
    await clinical_access.audit_clinical_read(
        session, principal=principal, ip=ip, resource=_RESOURCE, grants=[(consultation_id, grant)]
    )
    return response


async def list_for_invitee(
    session: AsyncSession, principal: Principal, *, ip: str | None
) -> list[InterconsultationForInvitee]:
    """Interconsultas asignadas a un médico invitado, con datos LIMITADOS (motivo, notas, edad y
    el video para unirse) — SIN identidad del paciente.

    El invitado es equipo tratante de ESE caso: ve motivo y notas si está habilitado para ejercer
    (`interconsultation_grant`). La lectura queda auditada con los ids de las consultas."""
    invited_doctor_id = principal.id
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
    # Una sola vía para todo el listado: el filtro de arriba ya garantiza que es el invitado.
    grant = clinical_access.interconsultation_grant(principal)
    response = [
        InterconsultationForInvitee.model_validate(r._asdict(), context=clinical_context(grant))
        for r in rows
    ]
    await clinical_access.audit_clinical_read(
        session,
        principal=principal,
        ip=ip,
        resource=_RESOURCE,
        grants=[(r.consultation_id, grant) for r in rows],
    )
    return response
