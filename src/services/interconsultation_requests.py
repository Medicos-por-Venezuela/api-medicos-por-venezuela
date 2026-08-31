"""Lógica de la interconsulta ASÍNCRONA (pacientes de consultorio).

El médico tratante documenta el caso de un paciente suyo —que no está en la plataforma— y pide
una segunda opinión: por especialidad (se difunde) o a un médico concreto. El primer especialista
que la toma gana; el contacto entre los dos ocurre FUERA de la plataforma.

No confundir con `interconsultations` (segunda opinión EN VIVO durante una consulta activa).
Ver .knowledge/interconsultas.md y tasks/interconsulta-asincrona/spec.md.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnprocessableError,
)
from src.models.doctor import Doctor
from src.models.interconsultation_request import InterconsultationRequest
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.schemas.interconsultation_request import (
    DoctorContact,
    InterconsultationRequestCreate,
    InterconsultationRequestInbox,
    InterconsultationRequestResponse,
    InterconsultationRequestTaken,
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


# --- Bandeja del especialista y toma del caso ---


async def inbox(
    session: AsyncSession, doctor_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[InterconsultationRequestInbox]:
    """Solicitudes ABIERTAS que este especialista puede tomar, ANONIMIZADAS.

    Son las de su especialidad más las dirigidas a él. Nunca las propias: pedir ayuda y
    ofrecerla son los dos lados del mismo feature, pero no sobre el mismo caso.
    """
    me = await session.get(Profile, doctor_id)
    mi_especialidad = me.specialty_id if me else None

    condiciones = [InterconsultationRequest.target_doctor_id == doctor_id]
    if mi_especialidad is not None:
        # Las difusiones llegan por especialidad; una dirigida a OTRO médico no se ve aunque
        # compartan especialidad — ya tiene destinatario elegido.
        condiciones.append(
            and_(
                InterconsultationRequest.specialty_id == mi_especialidad,
                InterconsultationRequest.target_doctor_id.is_(None),
            )
        )

    stmt = (
        select(InterconsultationRequest, Specialty.name, Patient.age_range)
        .join(Specialty, Specialty.id == InterconsultationRequest.specialty_id)
        .outerjoin(Patient, Patient.id == InterconsultationRequest.patient_id)
        .where(
            InterconsultationRequest.status == "open",
            InterconsultationRequest.requesting_doctor_id != doctor_id,
            or_(*condiciones),
        )
        .order_by(InterconsultationRequest.created_at.desc(), InterconsultationRequest.id)
        .offset(skip)
        .limit(limit)
    )
    return [
        InterconsultationRequestInbox(
            id=row.id,
            specialty_id=row.specialty_id,
            specialty_name=specialty_name,
            chief_complaint=row.chief_complaint,
            clinical_notes=row.clinical_notes,
            patient_age_range=age_range,
            dirigida_a_mi=row.target_doctor_id == doctor_id,
            created_at=row.created_at,
        )
        for row, specialty_name, age_range in (await session.execute(stmt)).all()
    ]


async def _puede_tomar(
    session: AsyncSession, row: InterconsultationRequest, doctor_id: uuid.UUID
) -> bool:
    """Elegibilidad para tomar: destinatario elegido, o médico de la especialidad pedida."""
    if row.target_doctor_id is not None:
        return row.target_doctor_id == doctor_id
    me = await session.get(Profile, doctor_id)
    return bool(me and me.specialty_id == row.specialty_id)


async def take(
    session: AsyncSession, request_id: uuid.UUID, doctor_id: uuid.UUID
) -> tuple[InterconsultationRequestTaken, dict | None]:
    """El especialista toma el caso. Devuelve (respuesta, args del correo al tratante).

    Dos especialistas pueden hacer clic en el mismo milisegundo. NUNCA select+update común: se
    bloquea la fila SI sigue 'open' con `with_for_update(nowait=True)`, así el perdedor recibe un
    error de lock inmediato (55P03 -> 409 por el manejador global) en vez de quedarse colgado
    esperando a que el otro termine. Mismo patrón que la cola (`services/queue.py`).
    """
    existe = await session.get(InterconsultationRequest, request_id)
    if existe is None:
        raise NotFoundError("Solicitud de interconsulta no encontrada.")
    if existe.requesting_doctor_id == doctor_id:
        raise ConflictError("No puedes tomar tu propia solicitud.")
    if not await _puede_tomar(session, existe, doctor_id):
        raise ForbiddenError("Esta interconsulta no es para tu especialidad.")

    row = (
        await session.execute(
            select(InterconsultationRequest)
            .where(
                InterconsultationRequest.id == request_id,
                InterconsultationRequest.status == "open",
            )
            .with_for_update(nowait=True)
        )
    ).scalar_one_or_none()
    if row is None:
        # La fila existe (se comprobó arriba) pero ya no está abierta: otro llegó primero, o el
        # tratante la canceló.
        raise ConflictError("Esta interconsulta ya no está disponible.")

    row.status = "taken"
    row.taken_by_doctor_id = doctor_id
    row.taken_at = datetime.now(UTC)

    tratante = await session.get(Profile, row.requesting_doctor_id)
    specialty_name = await session.scalar(
        select(Specialty.name).where(Specialty.id == row.specialty_id)
    )

    # Se audita la toma Y, con ella, la revelación del contacto: a partir de acá este
    # especialista tiene el teléfono de un colega (mismo criterio que `doctor.contact_viewed`).
    await audit.log_action(
        session,
        action="interconsultation_request.taken",
        actor_user_id=doctor_id,
        resource="interconsultation_requests",
        resource_id=row.id,
        metadata={"requesting_doctor_id": str(row.requesting_doctor_id)},
    )
    await session.commit()
    await session.refresh(row)

    respuesta = InterconsultationRequestTaken(
        id=row.id,
        status=row.status,
        taken_at=row.taken_at,
        specialty_name=specialty_name,
        chief_complaint=row.chief_complaint,
        clinical_notes=row.clinical_notes,
        patient_age_range=await session.scalar(
            select(Patient.age_range).where(Patient.id == row.patient_id)
        ),
        requesting_doctor=DoctorContact.model_validate(tratante),
    )

    especialista = await session.get(Profile, doctor_id)
    subject, text, html = notifications.interconsultation_taken_email(
        especialista.full_name if especialista else None,
        specialty_name or "",
        row.chief_complaint,
    )
    aviso = await notifications.doctor_event_email_args(
        session,
        user_id=row.requesting_doctor_id,
        event=TAKEN_EVENT,
        subject=subject,
        text=text,
        html=html,
    )
    return respuesta, aviso


async def taken_by_me(
    session: AsyncSession, doctor_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[InterconsultationRequestTaken]:
    """Casos ACTIVOS que tomó este especialista, con el contacto del tratante.

    Sin esta lista perdería ese contacto al recargar la página y el flujo se cortaría justo en el
    paso que lo justifica. El historial de casos ya cerrados es otra iteración.
    """
    stmt = (
        select(InterconsultationRequest, Specialty.name, Patient.age_range)
        .join(Specialty, Specialty.id == InterconsultationRequest.specialty_id)
        .outerjoin(Patient, Patient.id == InterconsultationRequest.patient_id)
        .where(
            InterconsultationRequest.taken_by_doctor_id == doctor_id,
            InterconsultationRequest.status == "taken",
        )
        .order_by(InterconsultationRequest.taken_at.desc(), InterconsultationRequest.id)
        .offset(skip)
        .limit(limit)
    )
    salida = []
    for row, specialty_name, age_range in (await session.execute(stmt)).all():
        tratante = await session.get(Profile, row.requesting_doctor_id)
        salida.append(
            InterconsultationRequestTaken(
                id=row.id,
                status=row.status,
                taken_at=row.taken_at,
                specialty_name=specialty_name,
                chief_complaint=row.chief_complaint,
                clinical_notes=row.clinical_notes,
                patient_age_range=age_range,
                requesting_doctor=DoctorContact.model_validate(tratante),
            )
        )
    return salida


# --- Transiciones terminales (exclusivas del médico TRATANTE) ---


async def _mia(
    session: AsyncSession, request_id: uuid.UUID, doctor_id: uuid.UUID
) -> InterconsultationRequest:
    row = await session.get(InterconsultationRequest, request_id)
    if row is None:
        raise NotFoundError("Solicitud de interconsulta no encontrada.")
    if row.requesting_doctor_id != doctor_id:
        raise ForbiddenError("Esta solicitud no es tuya.")
    return row


async def cancel(
    session: AsyncSession, request_id: uuid.UUID, doctor_id: uuid.UUID
) -> InterconsultationRequestResponse:
    """El tratante retira una solicitud que nadie tomó todavía."""
    row = await _mia(session, request_id, doctor_id)
    if row.status != "open":
        raise ConflictError("Solo se puede cancelar una solicitud abierta.")
    row.status = "cancelled"
    row.cancelled_at = datetime.now(UTC)
    await audit.log_action(
        session,
        action="interconsultation_request.cancelled",
        actor_user_id=doctor_id,
        resource="interconsultation_requests",
        resource_id=row.id,
    )
    await session.commit()
    await session.refresh(row)
    return await _to_response(session, row)


async def close(
    session: AsyncSession,
    request_id: uuid.UUID,
    doctor_id: uuid.UUID,
    closing_note: str | None = None,
) -> InterconsultationRequestResponse:
    """Cierra un caso ya tomado.

    Transición EXCLUSIVA del médico tratante: el especialista no cierra ni suelta el caso. Quien
    sabe si la ayuda sirvió es quien la pidió.
    """
    row = await _mia(session, request_id, doctor_id)
    if row.status != "taken":
        raise ConflictError("Solo se puede cerrar una solicitud que un especialista haya tomado.")
    row.status = "closed"
    row.closed_at = datetime.now(UTC)
    row.closing_note = closing_note
    await audit.log_action(
        session,
        action="interconsultation_request.closed",
        actor_user_id=doctor_id,
        resource="interconsultation_requests",
        resource_id=row.id,
    )
    await session.commit()
    await session.refresh(row)
    return await _to_response(session, row)
