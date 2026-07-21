"""Capa de negocio para consultations y sus eventos."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableError,
)
from src.models.consultation import CONSULTATION_STATUSES, Consultation
from src.models.consultation_event import ConsultationEvent
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.schemas.consultation import ConsultationCreate, ConsultationUpdate
from src.schemas.consultation_event import ConsultationEventCreate
from src.services import audit
from src.services.jitsi import new_room_url
from src.services.specialties import compute_priority

# Estados en los que la consulta sigue "viva" para el heartbeat del paciente.
_HEARTBEAT_OPEN_STATUSES = {"waiting", "in_progress"}
# Resultados de cierre permitidos.
_CLOSE_OUTCOMES = {"closed", "patient_no_show"}

# Panel médico: cola de espera (casos sin asignar) y "mis consultas abiertas".
_PANEL_WAITING_STATUSES = (
    "waiting",
    "in_progress",
    "referred_to_specialist",
    "urgent_in_person",
    "contacted_whatsapp",
    "patient_no_show",
)
_PANEL_MINE_STATUSES = ("in_progress", "contacted_whatsapp")


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
    """Lista consultas. Además de las filas `Consultation`, resuelve en el mismo
    query (LEFT JOIN) `patient_name` y `assigned_doctor_name` y los adjunta como
    atributos transitorios (no mapeados) a cada instancia, para que
    `ConsultationResponse` (from_attributes=True) los sirva sin round-trips extra
    (monitor de consultas del panel admin)."""
    _validate_status(status)
    stmt = (
        select(
            Consultation,
            Patient.full_name.label("patient_name"),
            Profile.full_name.label("assigned_doctor_name"),
        )
        .outerjoin(Patient, Consultation.patient_id == Patient.id)
        .outerjoin(Profile, Consultation.assigned_doctor_id == Profile.id)
    )
    if status:
        stmt = stmt.where(Consultation.status == status)
    if patient_id:
        stmt = stmt.where(Consultation.patient_id == patient_id)
    if not viewer_is_staff:
        # Un paciente solo ve las consultas ligadas a su propia cuenta (RLS select_own).
        stmt = stmt.where(Patient.user_id == viewer_user_id)
    stmt = stmt.order_by(Consultation.created_at.desc()).offset(skip).limit(limit)
    rows = (await session.execute(stmt)).all()
    consultations = []
    for row in rows:
        consultation = row.Consultation
        consultation.patient_name = row.patient_name
        consultation.assigned_doctor_name = row.assigned_doctor_name
        consultations.append(consultation)
    return consultations


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


def _ensure_can_manage(
    consultation: Consultation, actor_user_id: uuid.UUID | None, actor_is_admin: bool
) -> None:
    """Anti-IDOR (security.md): un médico solo gestiona consultas sin asignar o
    asignadas a sí mismo; los admin gestionan cualquiera. Va en el servicio, junto
    a la mutación, no en el router."""
    if actor_is_admin:
        return
    if consultation.assigned_doctor_id not in (None, actor_user_id):
        raise ConflictError("La consulta está asignada a otro médico.")


async def close_consultation(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    outcome: str,
    closed_by: uuid.UUID | None = None,
    note: str | None = None,
    actor_is_admin: bool = False,
) -> Consultation:
    """Cierra una consulta (`closed`) o la marca como ausencia (`patient_no_show`),
    guardando la nota y registrando el evento de auditoría (réplica de closeConsultation)."""
    if outcome not in _CLOSE_OUTCOMES:
        raise UnprocessableError(f"Resultado inválido. Permitidos: {sorted(_CLOSE_OUTCOMES)}")
    consultation = await get_consultation(session, consultation_id)
    _ensure_can_manage(consultation, closed_by, actor_is_admin)
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
    await audit.log_action(
        session,
        action="consultation.closed",
        actor_user_id=closed_by,
        resource="consultations",
        resource_id=consultation_id,
        metadata={"outcome": outcome},
    )
    await session.commit()
    await session.refresh(consultation)
    return consultation


async def claim_consultation(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    doctor_user_id: uuid.UUID,
    via_whatsapp: bool = False,
) -> Consultation:
    """Toma una consulta en espera para el médico autenticado.

    Claim ATÓMICO: el UPDATE solo matchea mientras `assigned_doctor_id IS NULL`, así que si
    otro médico la tomó primero afecta 0 filas y se responde 409. La condición de carrera la
    resuelve la base (un único ganador), no un read-then-write en la app."""
    consultation = await get_consultation(session, consultation_id)  # 404 si no existe
    now = datetime.now(UTC)
    stmt = (
        update(Consultation)
        .where(
            Consultation.id == consultation_id,
            Consultation.assigned_doctor_id.is_(None),
        )
        .values(
            status="in_progress",
            assigned_doctor_id=doctor_user_id,
            # No pisar opened_at si ya estaba (re-claim tras liberar).
            opened_at=func.coalesce(Consultation.opened_at, now),
            attended_via_whatsapp=via_whatsapp,
        )
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    if result.rowcount == 0:
        raise ConflictError("Este paciente ya fue tomado por otro médico.")

    session.add(
        ConsultationEvent(
            consultation_id=consultation_id,
            event_type="opened",
            created_by=doctor_user_id,
            note="Atendido vía WhatsApp" if via_whatsapp else "Abierta",
        )
    )
    await audit.log_action(
        session,
        action="consultation.claimed",
        actor_user_id=doctor_user_id,
        resource="consultations",
        resource_id=consultation_id,
        metadata={"via_whatsapp": via_whatsapp},
    )
    await session.commit()
    await session.refresh(consultation)  # el objeto quedó desfasado por el UPDATE en masa
    return consultation


async def get_panel(
    session: AsyncSession,
    doctor_user_id: uuid.UUID,
) -> tuple[list[Consultation], list[Consultation], int]:
    """Datos del panel médico en una pasada: cola de espera (TODA consulta sin asignar en un
    estado abierto — el médico las ve en tiempo real para atender de una vez, sin esperar), las
    consultas abiertas del propio médico y cuántas ha cerrado. El paciente viene precargado
    (`selectinload`) para el card de cada fila."""
    waiting_stmt = (
        select(Consultation)
        .options(selectinload(Consultation.patient), selectinload(Consultation.specialty_ref))
        .where(
            Consultation.assigned_doctor_id.is_(None),
            Consultation.status.in_(_PANEL_WAITING_STATUSES),
        )
        .order_by(Consultation.created_at.asc())
    )
    mine_stmt = (
        select(Consultation)
        .options(selectinload(Consultation.patient), selectinload(Consultation.specialty_ref))
        .where(
            Consultation.assigned_doctor_id == doctor_user_id,
            Consultation.status.in_(_PANEL_MINE_STATUSES),
        )
        .order_by(Consultation.created_at.asc())
    )
    closed_stmt = (
        select(func.count())
        .select_from(Consultation)
        .where(
            Consultation.assigned_doctor_id == doctor_user_id,
            Consultation.status == "closed",
        )
    )
    waiting = list((await session.execute(waiting_stmt)).scalars().all())
    mine = list((await session.execute(mine_stmt)).scalars().all())
    my_closed = (await session.execute(closed_stmt)).scalar_one()
    return waiting, mine, my_closed


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
    session: AsyncSession,
    consultation_id: uuid.UUID,
    data: ConsultationUpdate,
    actor_user_id: uuid.UUID | None = None,
    actor_is_admin: bool = False,
) -> Consultation:
    _validate_status(data.status)
    consultation = await get_consultation(session, consultation_id)
    _ensure_can_manage(consultation, actor_user_id, actor_is_admin)
    changes = data.model_dump(exclude_unset=True)
    # Un no-admin NO asigna consultas por PATCH: puede liberar la suya (None) o dejarla igual.
    # Tomar una consulta es SOLO vía el claim atómico (POST /{id}/claim o /queue/{id}/take):
    # un PATCH read-then-write reabriría la carrera que el claim resuelve en la base (dos
    # médicos concurrentes recibirían 200 y el último pisaría al primero en silencio).
    # doctor_id (ficha del médico) es server-only: lo escribe el backend/cola, no el cliente.
    if not actor_is_admin:
        if "doctor_id" in changes and changes["doctor_id"] != consultation.doctor_id:
            raise ConflictError("doctor_id lo asigna el sistema; no se edita por PATCH.")
        new_assigned = changes.get("assigned_doctor_id")
        if (
            "assigned_doctor_id" in changes
            and new_assigned is not None
            and new_assigned != consultation.assigned_doctor_id
        ):
            raise ConflictError(
                "Tomar una consulta es vía el claim atómico (POST /consultations/{id}/claim)."
            )
    for field, value in changes.items():
        setattr(consultation, field, value)
    await audit.log_action(
        session,
        action="consultation.updated",
        actor_user_id=actor_user_id,
        resource="consultations",
        resource_id=consultation_id,
        metadata={"fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(consultation)
    return consultation


async def delete_consultation(
    session: AsyncSession, consultation_id: uuid.UUID, deleted_by: uuid.UUID | None = None
) -> None:
    consultation = await get_consultation(session, consultation_id)
    await audit.log_action(
        session,
        action="consultation.deleted",
        actor_user_id=deleted_by,
        resource="consultations",
        resource_id=consultation_id,
    )
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
    actor_is_admin: bool = False,
) -> ConsultationEvent:
    consultation = await get_consultation(session, consultation_id)  # 404 si no existe
    # Mismo anti-IDOR que update/close: los eventos son el historial/auditoría del caso;
    # sin este check un médico podría inyectar un evento falso (p.ej. "closed") en la
    # consulta de otro.
    _ensure_can_manage(consultation, created_by, actor_is_admin)
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
