"""Capa de negocio para consultations y sus eventos."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
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
from src.services.specialties import (
    can_attend_consultation,
    compute_priority,
    flags_for_specialty_name,
)

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
    """Lista consultas. En el mismo query (LEFT JOIN) resuelve `patient_name` y
    `assigned_doctor_name` como atributos transitorios, y puebla la relación `patient`
    con la entidad ya cargada (sin N+1 ni lazy-load async), para que el detalle del panel
    admin (ConsultationDetailResponse) sirva el paciente anidado sin round-trips extra. Los
    response models que no tienen campo `patient` (ConsultationResponse/Patient) lo ignoran."""
    _validate_status(status)
    stmt = (
        select(
            Consultation,
            Patient,
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
        if row.Patient is not None:
            consultation.patient = row.Patient  # relación poblada desde el join
        consultation.patient_name = row.Patient.full_name if row.Patient else None
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
    if await session.get(Specialty, data.specialty_id) is None:
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
    signature: str | None = None,
    actor_is_admin: bool = False,
) -> Consultation:
    """Cierra una consulta (`closed`) o la marca como ausencia (`patient_no_show`), guardando la
    nota, la firma del médico (acto firmado, base para récipes) y el evento de auditoría."""
    if outcome not in _CLOSE_OUTCOMES:
        raise UnprocessableError(f"Resultado inválido. Permitidos: {sorted(_CLOSE_OUTCOMES)}")
    consultation = await get_consultation(session, consultation_id)
    _ensure_can_manage(consultation, closed_by, actor_is_admin)
    consultation.status = outcome
    consultation.closed_at = datetime.now(UTC)
    if note is not None:
        consultation.internal_note = note
    if signature is not None:
        consultation.close_signature = signature

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


def _ensure_future(scheduled_at: datetime) -> None:
    """Una cita solo se agenda hacia adelante (regla común a seguimiento y referencia)."""
    if scheduled_at <= datetime.now(UTC):
        raise UnprocessableError("La fecha de la cita debe ser futura.")


async def _add_scheduled_child(
    session: AsyncSession,
    parent: Consultation,
    *,
    scheduled_at: datetime,
    assigned_doctor_id: uuid.UUID | None,
    internal_note: str | None,
    event_note: str,
    actor_user_id: uuid.UUID | None,
) -> Consultation:
    """Crea la consulta HIJA agendada que continúa la cadena del padre (mismo paciente,
    especialidad, motivo y prioridad) más su evento `scheduled`. Lo comparten 'agendar
    seguimiento' (mismo médico) y 'agendar con especialista' (otro médico), que solo difieren
    en a quién se asigna y qué queda escrito. `code` lo pone el trigger de la BD."""
    child = Consultation(
        patient_id=parent.patient_id,
        assigned_doctor_id=assigned_doctor_id,
        specialty_id=parent.specialty_id,
        chief_complaint=parent.chief_complaint,
        category=parent.category,
        priority=parent.priority,
        status="scheduled",
        scheduled_at=scheduled_at,
        parent_consultation_id=parent.id,
        internal_note=internal_note,
    )
    session.add(child)
    await session.flush()
    session.add(
        ConsultationEvent(
            consultation_id=child.id,
            event_type="scheduled",
            created_by=actor_user_id,
            note=event_note,
        )
    )
    return child


async def schedule_follow_up(
    session: AsyncSession,
    *,
    parent_id: uuid.UUID,
    scheduled_at: datetime,
    closing_note: str | None,
    signature: str | None,
    actor_user_id: uuid.UUID | None,
    actor_is_admin: bool = False,
) -> Consultation:
    """Cierra la consulta padre (firmada) y crea una HIJA agendada para `scheduled_at`, continuando
    la cadena (mismo paciente, mismo médico). Todo en una transacción. Ver el módulo Agenda."""
    parent = await get_consultation(session, parent_id)
    _ensure_can_manage(parent, actor_user_id, actor_is_admin)
    _ensure_future(scheduled_at)

    # 1) Cerrar el padre (firmado).
    parent.status = "closed"
    parent.closed_at = datetime.now(UTC)
    if closing_note is not None:
        parent.internal_note = closing_note
    if signature is not None:
        parent.close_signature = signature
    session.add(
        ConsultationEvent(
            consultation_id=parent.id,
            event_type="closed",
            created_by=actor_user_id,
            note=closing_note,
        )
    )

    # 2) Crear la hija agendada, con el MISMO médico (continúa la cadena).
    child = await _add_scheduled_child(
        session,
        parent,
        scheduled_at=scheduled_at,
        assigned_doctor_id=parent.assigned_doctor_id,
        internal_note=None,
        event_note=f"Seguimiento agendado para {scheduled_at.isoformat()}",
        actor_user_id=actor_user_id,
    )
    await audit.log_action(
        session,
        action="consultation.follow_up_scheduled",
        actor_user_id=actor_user_id,
        resource="consultations",
        resource_id=child.id,
        metadata={"parent_id": str(parent.id), "scheduled_at": scheduled_at.isoformat()},
    )
    await session.commit()
    await session.refresh(child)
    return child


async def schedule_referral(
    session: AsyncSession,
    *,
    parent_id: uuid.UUID,
    invited_doctor_id: uuid.UUID,
    scheduled_at: datetime,
    reason: str,
    signature: str | None,
    actor_user_id: uuid.UUID | None,
    actor_is_admin: bool = False,
) -> Consultation:
    """Agendar con especialista (REFERENCIA): entrega la consulta a OTRO médico. El padre queda
    'referred_to_specialist' (ya no lo atiende el médico actual) y se crea una HIJA agendada
    asignada al médico invitado, con el motivo firmado. El referido ve las notas previas (chain).
    Distinto de 'Agendar seguimiento' (mismo médico) y de una Interconsulta (en vivo, limitada)."""
    parent = await get_consultation(session, parent_id)
    _ensure_can_manage(parent, actor_user_id, actor_is_admin)
    _ensure_future(scheduled_at)
    if invited_doctor_id == parent.assigned_doctor_id:
        raise ConflictError("El especialista debe ser otro médico (usa 'Agendar seguimiento').")
    invited = await session.get(Profile, invited_doctor_id)
    if invited is None or invited.role not in ("doctor", "specialist"):
        raise UnprocessableError("El médico especialista no es válido.")

    # 1) Entregar el padre: queda derivado al especialista (firmado con el motivo).
    parent.status = "referred_to_specialist"
    if signature is not None:
        parent.close_signature = signature
    session.add(
        ConsultationEvent(
            consultation_id=parent.id,
            event_type="referred_to_specialist",
            created_by=actor_user_id,
            note=reason,
        )
    )

    # 2) Crear la hija agendada asignada al especialista (continúa la cadena). El motivo va en
    #    internal_note para que el referido lo vea; las notas previas van por el chain.
    child = await _add_scheduled_child(
        session,
        parent,
        scheduled_at=scheduled_at,
        assigned_doctor_id=invited_doctor_id,
        internal_note=reason,
        event_note=(
            f"Referencia a especialista agendada para {scheduled_at.isoformat()}: {reason}"
        ),
        actor_user_id=actor_user_id,
    )
    await audit.log_action(
        session,
        action="consultation.referred_to_specialist",
        actor_user_id=actor_user_id,
        resource="consultations",
        resource_id=child.id,
        metadata={
            "parent_id": str(parent.id),
            "invited_doctor_id": str(invited_doctor_id),
            "scheduled_at": scheduled_at.isoformat(),
        },
    )
    await session.commit()
    await session.refresh(child)
    return child


async def list_agenda(
    session: AsyncSession,
    *,
    doctor_user_id: uuid.UUID | None = None,
    patient_user_id: uuid.UUID | None = None,
) -> list[Consultation]:
    """Citas AGENDADAS (scheduled_at no nulo, status 'scheduled') por fecha ascendente. Filtra por
    médico asignado (su agenda) o por paciente (la suya). Adjunta patient_name/assigned_doctor_name
    como transitorios (igual que list_consultations) para ConsultationResponse."""
    stmt = (
        select(
            Consultation,
            Patient.full_name.label("patient_name"),
            Profile.full_name.label("assigned_doctor_name"),
        )
        .outerjoin(Patient, Consultation.patient_id == Patient.id)
        .outerjoin(Profile, Consultation.assigned_doctor_id == Profile.id)
        .where(Consultation.scheduled_at.isnot(None), Consultation.status == "scheduled")
    )
    if doctor_user_id is not None:
        stmt = stmt.where(Consultation.assigned_doctor_id == doctor_user_id)
    if patient_user_id is not None:
        stmt = stmt.where(Patient.user_id == patient_user_id)
    stmt = stmt.order_by(Consultation.scheduled_at.asc())
    rows = (await session.execute(stmt)).all()
    out = []
    for row in rows:
        c = row.Consultation
        c.patient_name = row.patient_name
        c.assigned_doctor_name = row.assigned_doctor_name
        out.append(c)
    return out


async def get_chain(session: AsyncSession, consultation_id: uuid.UUID) -> list[Consultation]:
    """Toda la cadena de seguimiento (raíz + descendientes) a la que pertenece la consulta. Sube a
    la raíz por parent_consultation_id y baja por BFS a las hijas, ordenado."""
    current = await session.get(Consultation, consultation_id)
    if current is None:
        raise NotFoundError("Consulta no encontrada.")
    root = current
    guard: set = set()
    while root.parent_consultation_id is not None and root.id not in guard:
        guard.add(root.id)
        parent = await session.get(Consultation, root.parent_consultation_id)
        if parent is None:
            break
        root = parent
    chain: list[Consultation] = []
    queue = [root]
    seen: set = set()
    while queue:
        node = queue.pop(0)
        if node.id in seen:
            continue
        seen.add(node.id)
        chain.append(node)
        children_stmt = (
            select(Consultation)
            .where(Consultation.parent_consultation_id == node.id)
            .order_by(Consultation.created_at.asc())
        )
        queue.extend((await session.scalars(children_stmt)).all())
    return chain


async def claim_consultation(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    doctor_user_id: uuid.UUID,
    via_whatsapp: bool = False,
    doctor_specialty: str | None = None,
    is_admin: bool = False,
) -> Consultation:
    """Toma una consulta en espera para el médico autenticado.

    Claim ATÓMICO: el UPDATE solo matchea mientras `assigned_doctor_id IS NULL`, así que si
    otro médico la tomó primero afecta 0 filas y se responde 409. La condición de carrera la
    resuelve la base (un único ganador), no un read-then-write en la app.

    Valida la especialidad: la separación psicología <-> salud física es una regla de negocio,
    y este endpoint es el ÚNICO camino real para tomar un caso. Que el panel ya filtre la lista
    no basta — un POST directo se saltaba el filtro por completo."""
    consultation = await get_consultation(session, consultation_id)  # 404 si no existe
    if not is_admin:
        # La reserva de salud mental sale del catálogo (columnas de `specialties`), no de una
        # lista de nombres: renombrar una especialidad ya no puede abrirla en silencio.
        caso = await session.get(Specialty, consultation.specialty_id)
        if not can_attend_consultation(
            doctor=await flags_for_specialty_name(session, doctor_specialty),
            consultation_is_mental_health=bool(caso and caso.is_mental_health),
        ):
            raise ForbiddenError("Este caso no corresponde a tu especialidad.")
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
    doctor_specialty: str | None = None,
    is_admin: bool = False,
) -> tuple[list[Consultation], list[Consultation], int]:
    """Datos del panel médico en una pasada: cola de espera ACOTADA a lo que este médico puede
    atender, las consultas abiertas del propio médico y cuántas ha cerrado. El paciente viene
    precargado (`selectinload`) para el card de cada fila.

    El filtro por especialidad se aplica AQUÍ, en el servidor: antes se devolvía la cola entera
    y el recorte era cosmético en el cliente, así que un psicólogo veía (y podía tomar) la
    cédula, el teléfono y el motivo de un caso de medicina general. La regla es la misma que ya
    usaba la elegibilidad (`can_attend_consultation`), y `claim_consultation` la revalida: el
    filtro de una lista nunca es un control de acceso por sí solo.

    El admin sigue viendo la cola completa. El orden es FIFO (más antiguo primero)."""
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
    if not is_admin:
        # En Python y no en SQL a propósito: es la MISMA función que valida el claim, y
        # duplicarla en SQL sería la forma de que las dos se desincronicen. La cola sin asignar
        # es corta, así que el coste es irrelevante.
        # Una sola consulta para los flags del médico; los del caso vienen con `specialty_ref`,
        # que ya se precarga arriba (sin N+1).
        doctor_flags = await flags_for_specialty_name(session, doctor_specialty)
        waiting = [
            c
            for c in waiting
            if can_attend_consultation(
                doctor=doctor_flags,
                consultation_is_mental_health=bool(
                    c.specialty_ref and c.specialty_ref.is_mental_health
                ),
            )
        ]
    mine = list((await session.execute(mine_stmt)).scalars().all())
    my_closed = (await session.execute(closed_stmt)).scalar_one()
    return waiting, mine, my_closed


# `heartbeat` se eliminó: era el único escritor de `patient_last_seen_at` y no lo llamaba
# ningún cliente. La presencia del paciente en sala la resuelve Realtime Presence
# (lib/patientPresence.tsx), que la reemplazó precisamente para no hacer un UPDATE cada 15 s
# por paciente en espera. La columna se conserva: tiene datos históricos de producción.


async def mark_entered_call(session: AsyncSession, consultation_id: uuid.UUID) -> Consultation:
    """Marca que el paciente entró a la videollamada (`entered_call_at`, idempotente), solo si
    sigue en espera o en progreso. Reemplaza la RPC mark_patient_entered_call (el bump de
    patient_last_seen_at quedó obsoleto: la presencia la maneja Realtime Presence)."""
    consultation = await get_consultation(session, consultation_id)
    if consultation.status in _HEARTBEAT_OPEN_STATUSES and consultation.entered_call_at is None:
        consultation.entered_call_at = datetime.now(UTC)
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
    """Eventos del caso con el AUTOR resuelto (join con users → author_name/author_role), para que
    el frontend no lea `users` directo. Los nombres se adjuntan como transitorios (igual que
    list_agenda) y ConsultationEventResponse (from_attributes) los toma."""
    await get_consultation(session, consultation_id)  # 404 si no existe
    stmt = (
        select(
            ConsultationEvent,
            Profile.full_name.label("author_name"),
            Profile.role.label("author_role"),
        )
        .outerjoin(Profile, ConsultationEvent.created_by == Profile.id)
        .where(ConsultationEvent.consultation_id == consultation_id)
        .order_by(ConsultationEvent.created_at.asc())
    )
    rows = (await session.execute(stmt)).all()
    out = []
    for row in rows:
        event = row.ConsultationEvent
        event.author_name = row.author_name
        event.author_role = row.author_role
        out.append(event)
    return out


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
