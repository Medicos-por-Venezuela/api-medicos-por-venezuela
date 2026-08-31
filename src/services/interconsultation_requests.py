"""Lógica de la interconsulta ASÍNCRONA (pacientes de consultorio).

El médico tratante documenta el caso de un paciente suyo —que no está en la plataforma— y pide
una segunda opinión: por especialidad (se difunde) o a un médico concreto. El primer especialista
que la toma gana; el contacto entre los dos ocurre FUERA de la plataforma.

No confundir con `interconsultations` (segunda opinión EN VIVO durante una consulta activa).
Ver .knowledge/interconsultas.md y tasks/interconsulta-asincrona/spec.md.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError, UnprocessableError
from src.models.doctor import Doctor
from src.models.interconsultation_request import InterconsultationRequest
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.schemas.interconsultation_request import (
    DoctorContact,
    InterconsultationRequestCreate,
    InterconsultationRequestResponse,
)
from src.services import audit, notifications
from src.services import patients as patients_service

BROADCAST_EVENT = "interconsultation_request_broadcast"
TAKEN_EVENT = "interconsultation_request_taken"


# --- Elegibilidad ---


async def _specialty_pedible(session: AsyncSession, specialty_id: uuid.UUID) -> Specialty:
    """La especialidad existe y se puede pedir en una interconsulta.

    Medicina general está fuera por diseño: pedirle ayuda a otro general no es una
    interconsulta. La regla sale de la COLUMNA `available_for_interconsultation`, nunca de
    comparar el nombre (ver la migración 20260831_174358).
    """
    specialty = await session.get(Specialty, specialty_id)
    if specialty is None or specialty.deleted_at is not None:
        raise NotFoundError("Especialidad no encontrada.")
    if not specialty.available_for_interconsultation:
        raise UnprocessableError(
            f"'{specialty.name}' no es una especialidad que se pueda pedir en una interconsulta."
        )
    return specialty


def _doctor_habilitado(profile: Profile | None, doctor: Doctor | None) -> bool:
    """Un médico al que tiene sentido mandarle un caso: cuenta activa y verificada, con ficha
    válida para ejercer. Mismo criterio que el gate de credencial — no se le difunde un caso a
    quien el backend no dejaría atenderlo."""
    return bool(
        profile
        and profile.active
        and profile.verified
        and doctor
        and doctor.deleted_at is None
        and doctor.verified
        and doctor.status == 1
    )


async def _resolver_destinatario(
    session: AsyncSession, target_doctor_id: uuid.UUID, requesting_doctor_id: uuid.UUID
) -> tuple[Profile, uuid.UUID]:
    """Valida el médico elegido en modo 'doctor' y devuelve (perfil, specialty_id derivada)."""
    if target_doctor_id == requesting_doctor_id:
        raise ConflictError("No puedes pedirte una interconsulta a vos mismo.")
    profile = await session.get(Profile, target_doctor_id)
    doctor = await session.scalar(select(Doctor).where(Doctor.user_id == target_doctor_id))
    if not _doctor_habilitado(profile, doctor):
        raise UnprocessableError("Ese médico no está habilitado para recibir interconsultas.")
    if profile.specialty_id is None:
        raise UnprocessableError("Ese médico no tiene una especialidad registrada.")
    return profile, profile.specialty_id


async def _destinatarios(
    session: AsyncSession,
    *,
    specialty_id: uuid.UUID,
    requesting_doctor_id: uuid.UUID,
    target_doctor_id: uuid.UUID | None,
) -> list[str]:
    """Correos a los que difundir, ya filtrados por elegibilidad y por opt-out.

    En modo 'doctor' es uno solo. En modo 'specialty' son todos los médicos habilitados de esa
    especialidad, menos quien pide (avisarle de su propio caso sería ruido).
    """
    stmt = (
        select(Profile.email, Profile.notification_prefs)
        .join(Doctor, Doctor.user_id == Profile.id)
        .where(
            Profile.email.is_not(None),
            Profile.active.is_(True),
            Profile.verified.is_(True),
            Profile.id != requesting_doctor_id,
            Doctor.deleted_at.is_(None),
            Doctor.verified.is_(True),
            Doctor.status == 1,
        )
    )
    if target_doctor_id is not None:
        stmt = stmt.where(Profile.id == target_doctor_id)
    else:
        stmt = stmt.where(Profile.specialty_id == specialty_id)

    return [
        email
        for email, prefs in (await session.execute(stmt)).all()
        if notifications.should_send(prefs, BROADCAST_EVENT, "email")
    ]


# --- Respuestas ---


async def _contacto(session: AsyncSession, user_id: uuid.UUID | None) -> DoctorContact | None:
    if user_id is None:
        return None
    profile = await session.get(Profile, user_id)
    return DoctorContact.model_validate(profile) if profile else None


async def _to_response(
    session: AsyncSession, row: InterconsultationRequest
) -> InterconsultationRequestResponse:
    """Vista del médico TRATANTE. Incluye el nombre de su paciente (es suyo) y el contacto de
    los colegas involucrados."""
    return InterconsultationRequestResponse(
        id=row.id,
        patient_id=row.patient_id,
        patient_name=await session.scalar(
            select(Patient.full_name).where(Patient.id == row.patient_id)
        ),
        mode=row.mode,
        specialty_id=row.specialty_id,
        specialty_name=await session.scalar(
            select(Specialty.name).where(Specialty.id == row.specialty_id)
        ),
        chief_complaint=row.chief_complaint,
        clinical_notes=row.clinical_notes,
        status=row.status,
        notified_count=row.notified_count,
        created_at=row.created_at,
        taken_at=row.taken_at,
        closed_at=row.closed_at,
        cancelled_at=row.cancelled_at,
        target_doctor=await _contacto(session, row.target_doctor_id),
        taken_by=await _contacto(session, row.taken_by_doctor_id),
    )


# --- Casos de uso ---


async def create_request(
    session: AsyncSession,
    data: InterconsultationRequestCreate,
    requesting_doctor_id: uuid.UUID,
) -> tuple[InterconsultationRequestResponse, dict]:
    """Crea la solicitud y devuelve (respuesta, payload del correo de difusión).

    El payload sale resuelto a valores planos porque el envío se encola con BackgroundTasks y
    corre DESPUÉS de cerrar la request, cuando la sesión ya no existe (mismo patrón que
    `notifications.appointment_email_args`).
    """
    # El caso tiene que ser de un paciente suyo: 404 si no existe, 403 si es de otro médico.
    patient = await patients_service.get_doctor_patient(
        session, data.patient_id, doctor_id=requesting_doctor_id
    )

    if data.mode == "doctor":
        _, specialty_id = await _resolver_destinatario(
            session, data.target_doctor_id, requesting_doctor_id
        )
        # Se valida igual que en modo especialidad: elegir a dedo a un médico general seguiría
        # sin ser una interconsulta.
        specialty = await _specialty_pedible(session, specialty_id)
    else:
        specialty = await _specialty_pedible(session, data.specialty_id)
        specialty_id = specialty.id

    destinatarios = await _destinatarios(
        session,
        specialty_id=specialty_id,
        requesting_doctor_id=requesting_doctor_id,
        target_doctor_id=data.target_doctor_id,
    )

    row = InterconsultationRequest(
        patient_id=patient.id,
        requesting_doctor_id=requesting_doctor_id,
        mode=data.mode,
        specialty_id=specialty_id,
        target_doctor_id=data.target_doctor_id,
        chief_complaint=data.chief_complaint,
        clinical_notes=data.clinical_notes,
        notified_count=len(destinatarios),
    )
    session.add(row)
    await session.flush()

    await audit.log_action(
        session,
        action="interconsultation_request.created",
        actor_user_id=requesting_doctor_id,
        resource="interconsultation_requests",
        resource_id=row.id,
        metadata={
            "mode": row.mode,
            "specialty_id": str(specialty_id),
            "notified_count": row.notified_count,
        },
    )
    # SIN este commit la solicitud NO se guarda: `get_db` cierra la sesión al terminar el request
    # y eso hace ROLLBACK. Ya pasó con `interconsultations` (201 con id real y cero filas en la
    # tabla); no se repite.
    await session.commit()
    await session.refresh(row)

    subject, text, html = notifications.interconsultation_broadcast_email(
        specialty.name, row.chief_complaint, patient.age_range
    )
    difusion = {
        "recipients": destinatarios,
        "subject": subject,
        "text": text,
        "html": html,
        "category": BROADCAST_EVENT.replace("_", "-"),
    }
    return await _to_response(session, row), difusion


async def list_mine(
    session: AsyncSession, requesting_doctor_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[InterconsultationRequestResponse]:
    """Las solicitudes del médico tratante, más recientes primero."""
    stmt = (
        select(InterconsultationRequest)
        .where(InterconsultationRequest.requesting_doctor_id == requesting_doctor_id)
        # `id` como desempate: sin columna única al final, dos solicitudes del mismo instante
        # pueden repetirse u omitirse entre páginas con OFFSET.
        .order_by(InterconsultationRequest.created_at.desc(), InterconsultationRequest.id)
        .offset(skip)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [await _to_response(session, row) for row in rows]
