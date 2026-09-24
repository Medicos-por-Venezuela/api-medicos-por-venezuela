"""Capa de negocio para consultations y sus eventos."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.clinical_crypto import Sealed
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
from src.services import audit, queue_access
from src.services.doctors import practicing_doctor_exists
from src.services.jitsi import new_room_url
from src.services.specialties import compute_priority

if TYPE_CHECKING:  # security -> services/__init__ -> consultations: import circular en runtime
    from src.core.security import Principal

# Estados en los que la consulta sigue "viva" para el heartbeat del paciente.
_HEARTBEAT_OPEN_STATUSES = {"waiting", "in_progress"}
# Resultados de cierre permitidos.
_CLOSE_OUTCOMES = {"closed", "patient_no_show"}

# Panel médico: "mis consultas abiertas". La cola es `waiting` sin asignar (ver get_panel).
_PANEL_MINE_STATUSES = ("in_progress", "contacted_whatsapp")
# Un caso ya tomado que sigue abierto: se le puede crear sala y se puede derivar.
_OPEN_ASSIGNED_STATUSES = ("in_progress", "contacted_whatsapp")
# Estados en los que todavía tiene sentido crear la sala de video.
_ROOM_STATUSES = ("waiting", *_OPEN_ASSIGNED_STATUSES)
# Evento que deja escrito quién derivó un caso a otra cola y por qué.
DERIVED_EVENT = "derived"
# Traza operativa que deja el panel (admin o médico) al cambiar estado, médico o especialidad.
ADMIN_UPDATE_EVENT = "admin_update"
# Campos clínicos que se escriben por PATCH: solo los toca el médico tratante (ni el admin).
_CLINICAL_UPDATE_FIELDS = frozenset({"chief_complaint", "clinical_notes", "internal_note"})


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
        .options(
            selectinload(Consultation.specialty_ref), selectinload(Consultation.derived_from_ref)
        )
    )
    if status:
        stmt = stmt.where(Consultation.status == status)
    if patient_id:
        stmt = stmt.where(Consultation.patient_id == patient_id)
    if not viewer_is_staff:
        # Un paciente solo ve las consultas ligadas a su propia cuenta (RLS select_own).
        stmt = stmt.where(Patient.user_id == viewer_user_id)
    # Desempate por id: consultas creadas en la misma transacción comparten created_at, y sin
    # columna única el OFFSET de "Cargar más" en admin/pacientes podía repetir u omitir casos.
    stmt = (
        stmt.order_by(Consultation.created_at.desc(), Consultation.id.desc())
        .offset(skip)
        .limit(limit)
    )
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


async def get_consultation_detail(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    viewer_is_staff: bool = True,
    viewer_user_id: uuid.UUID | None = None,
) -> Consultation:
    """`get_consultation` (con su control de pertenencia) más los nombres de la especialidad
    actual y de la que viene derivada, para el detalle. `populate_existing` porque la fila puede
    estar ya en la sesión sin esas relaciones (son `noload`)."""
    await get_consultation(session, consultation_id, viewer_is_staff, viewer_user_id)
    stmt = (
        select(Consultation)
        .options(
            selectinload(Consultation.specialty_ref), selectinload(Consultation.derived_from_ref)
        )
        .where(Consultation.id == consultation_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


async def belongs_to_patient(
    session: AsyncSession, consultation_id: uuid.UUID, user_id: uuid.UUID | None
) -> bool:
    """¿Esta consulta es del paciente con esta cuenta? Sin lanzar; `False` también si no existe.

    Es la MISMA regla de pertenencia que `get_consultation` para un no-staff (`Patient.user_id`),
    extraída porque ahora hace falta como **credencial** y no solo como filtro de lectura: el
    paciente que vuelve por `/mi-caso` tiene sesión pero no el token de sala que su día se le
    entregó por la URL, y sin esto no podía ni entrar a su propia videoconsulta.
    """
    if user_id is None:
        return False
    stmt = (
        select(Patient.user_id)
        .join(Consultation, Consultation.patient_id == Patient.id)
        .where(Consultation.id == consultation_id)
    )
    return (await session.scalar(stmt)) == user_id


async def create_consultation(session: AsyncSession, data: ConsultationCreate) -> Consultation:
    _validate_status(data.status)
    patient = await session.get(Patient, data.patient_id)
    if patient is None:
        raise BadRequestError("El paciente referenciado (patient_id) no existe.")
    specialty = await session.get(Specialty, data.specialty_id)
    if specialty is None:
        raise BadRequestError("La especialidad referenciada (specialty_id) no existe.")
    if specialty.is_placeholder:
        # "Otra" no es la cola de nadie: sus médicos no ven casos hasta definir su especialidad.
        raise UnprocessableError("Elige la especialidad que necesitas o Medicina general.")
    # code lo asigna SIEMPRE el trigger generate_consultation_code en la base.
    consultation = Consultation(**data.model_dump())

    # Derivación de campos desde las necesidades del paciente (igual que el registro
    # del frontend), solo cuando no vienen explícitos.
    needs = patient.needs_tags or []
    if consultation.category is None and needs:
        consultation.category = needs[0]
    if consultation.chief_complaint is None:
        # `patient.description` es un `Sealed` de otra columna: se asigna tal cual y el tipo lo
        # re-cifra para `consultations.chief_complaint` (el AAD ata cada texto a su columna).
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


def _ensure_can_write_clinical(
    consultation: Consultation, actor_user_id: uuid.UUID | None, actor_practices: bool
) -> None:
    """Escribir texto clínico (motivo, notas, nota de cierre, motivo de derivación, eventos con
    nota) es del médico tratante: habilitado (`actor_practices`) y asignado a ESTE caso. El admin
    gestiona estado/asignación/`nota_admin`, pero no escribe (ni pisa) la nota del médico.
    Va después de `_ensure_can_manage`: un médico sobre un caso ajeno sigue recibiendo 409."""
    if not actor_practices or consultation.assigned_doctor_id != actor_user_id:
        raise ForbiddenError("Solo el médico que atiende el caso escribe sus notas clínicas.")


async def close_consultation(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    outcome: str,
    closed_by: uuid.UUID | None = None,
    note: str | None = None,
    signature: str | None = None,
    actor_is_admin: bool = False,
    actor_practices: bool = False,
) -> Consultation:
    """Cierra una consulta (`closed`) o la marca como ausencia (`patient_no_show`), guardando la
    nota, la firma del médico (acto firmado, base para récipes) y el evento de auditoría. La
    nota es clínica: solo con el médico tratante (un admin cierra sin nota)."""
    if outcome not in _CLOSE_OUTCOMES:
        raise UnprocessableError(f"Resultado inválido. Permitidos: {sorted(_CLOSE_OUTCOMES)}")
    consultation = await get_consultation(session, consultation_id)
    _ensure_can_manage(consultation, closed_by, actor_is_admin)
    if note is not None:
        _ensure_can_write_clinical(consultation, closed_by, actor_practices)
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
        # `Sealed` tal cual: misma columna, se guarda el mismo texto cifrado sin descifrarlo.
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
    actor_practices: bool = False,
) -> Consultation:
    """Cierra la consulta padre (firmada) y crea una HIJA agendada para `scheduled_at`, continuando
    la cadena (mismo paciente, mismo médico). Todo en una transacción. Ver el módulo Agenda.
    La nota de cierre es clínica: solo con el médico tratante."""
    parent = await get_consultation(session, parent_id)
    _ensure_can_manage(parent, actor_user_id, actor_is_admin)
    if closing_note is not None:
        _ensure_can_write_clinical(parent, actor_user_id, actor_practices)
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
    actor_practices: bool = False,
) -> Consultation:
    """Agendar con especialista (REFERENCIA): entrega la consulta a OTRO médico. El padre queda
    'referred_to_specialist' (ya no lo atiende el médico actual) y se crea una HIJA agendada
    asignada al médico invitado, con el motivo firmado. El referido ve las notas previas (chain).
    Distinto de 'Agendar seguimiento' (mismo médico) y de una Interconsulta (en vivo, limitada).
    El motivo es clínico: solo refiere el médico tratante."""
    parent = await get_consultation(session, parent_id)
    _ensure_can_manage(parent, actor_user_id, actor_is_admin)
    _ensure_can_write_clinical(parent, actor_user_id, actor_practices)
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


def lineage_ids(chain: list[Consultation], consultation_id: uuid.UUID) -> set[uuid.UUID]:
    """El caso pedido y sus ANCESTROS dentro de la cadena (sube por `parent_consultation_id`).

    Es lo que hereda el médico que trata el caso pedido: las notas previas que llevaron a él. Las
    hijas y las ramas hermanas (p. ej. la derivación que tomó otro especialista) no: esas son de
    su propio equipo tratante."""
    by_id = {c.id: c for c in chain}
    lineage: set[uuid.UUID] = set()
    current = by_id.get(consultation_id)
    while current is not None and current.id not in lineage:
        lineage.add(current.id)
        parent_id = current.parent_consultation_id
        current = by_id.get(parent_id) if parent_id is not None else None
    return lineage


async def claim_consultation(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    doctor_user_id: uuid.UUID,
    doctor_specialty_id: uuid.UUID | None = None,
    is_admin: bool = False,
) -> Consultation:
    """Toma una consulta en espera para el médico autenticado, con su sala de video.

    Claim ATÓMICO: el UPDATE solo matchea mientras el caso sigue `waiting` y sin asignar, así
    que si otro médico lo tomó primero afecta 0 filas y se responde 409. La condición de carrera
    la resuelve la base (un único ganador), no un read-then-write en la app.

    La sala se fija en ese MISMO UPDATE (`coalesce` con una sala nueva): la atención es siempre
    por videoconsulta, y crear la sala en otra llamada antes del claim es lo que dejaba casos
    tomados sin enlace cuando esa llamada fallaba.

    Valida la especialidad con `queue_access`, la misma regla que arma la lista: que el panel
    filtre no basta, un POST directo se saltaría el filtro."""
    consultation = await get_consultation(session, consultation_id)  # 404 si no existe
    await queue_access.ensure_can_take(
        session,
        consultation,
        user_id=doctor_user_id,
        specialty_id=doctor_specialty_id,
        is_admin=is_admin,
    )
    now = datetime.now(UTC)
    stmt = (
        update(Consultation)
        .where(
            Consultation.id == consultation_id,
            Consultation.status == "waiting",
            Consultation.assigned_doctor_id.is_(None),
        )
        .values(
            status="in_progress",
            assigned_doctor_id=doctor_user_id,
            # No pisar opened_at si ya estaba (re-claim tras liberar).
            opened_at=func.coalesce(Consultation.opened_at, now),
            attended_via_whatsapp=False,
            video_room_url=func.coalesce(Consultation.video_room_url, new_room_url()),
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
            note="Abierta",
        )
    )
    await audit.log_action(
        session,
        action="consultation.claimed",
        actor_user_id=doctor_user_id,
        resource="consultations",
        resource_id=consultation_id,
    )
    await session.commit()
    await session.refresh(consultation)  # el objeto quedó desfasado por el UPDATE en masa
    return consultation


async def start_scheduled_consultation(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    actor_is_admin: bool = False,
) -> Consultation:
    """Abre una cita agendada: `scheduled` → `in_progress` y le crea la sala de video si falta.

    Es el equivalente al claim de la cola, pero para la Agenda: la hija agendada ya existe y ya
    tiene médico (`schedule_follow_up`/`refer` la asignan), así que no hay carrera por tomarla —
    la hay contra el doble clic, y la resuelve el UPDATE condicional sobre `status == 'scheduled'`:
    la segunda petición afecta 0 filas y recibe 409 en vez de duplicar el evento.

    Sin este paso `ensure_video_room` responde 409 ("La consulta ya no está abierta."): `scheduled`
    no está entre sus estados. El correo al paciente lo encola el router con la sala ya creada."""
    consultation = await get_consultation(session, consultation_id)
    _ensure_can_manage(consultation, actor_user_id, actor_is_admin)
    now = datetime.now(UTC)
    values: dict = {
        "status": "in_progress",
        "opened_at": func.coalesce(Consultation.opened_at, now),
        "video_room_url": func.coalesce(Consultation.video_room_url, new_room_url()),
    }
    if actor_user_id is not None:
        # Una cita sin médico (dato legacy/manual) queda asignada a quien la atiende: sin
        # `assigned_doctor_id` el paciente no ve la sala (`phase_of` exige médico Y sala).
        values["assigned_doctor_id"] = func.coalesce(
            Consultation.assigned_doctor_id, actor_user_id
        )
    result = await session.execute(
        update(Consultation)
        .where(
            Consultation.id == consultation_id,
            Consultation.status == "scheduled",
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        raise ConflictError("Esta cita ya no está agendada.")
    session.add(
        ConsultationEvent(
            consultation_id=consultation_id,
            event_type="opened",
            created_by=actor_user_id,
            note="Cita agendada iniciada",
        )
    )
    await audit.log_action(
        session,
        action="consultation.started",
        actor_user_id=actor_user_id,
        resource="consultations",
        resource_id=consultation_id,
    )
    await session.commit()
    await session.refresh(consultation)
    return consultation


def _with_specialty_names(stmt):
    """Precarga el paciente y los nombres de especialidad (actual y de origen) de una lista.

    `populate_existing`: si la fila ya estaba en la sesión (la tomó o derivó esta misma sesión),
    sin esto SQLAlchemy devuelve el objeto con las relaciones `noload` vacías y la especialidad
    saldría en blanco. Son lecturas: no hay cambios pendientes que pisar."""
    return stmt.options(
        selectinload(Consultation.patient),
        selectinload(Consultation.specialty_ref),
        selectinload(Consultation.derived_from_ref),
    ).execution_options(populate_existing=True)


async def get_panel(
    session: AsyncSession,
    doctor_user_id: uuid.UUID,
    doctor_specialty_id: uuid.UUID | None = None,
    is_admin: bool = False,
) -> tuple[list[Consultation], list[Consultation], int, queue_access.QueueScope]:
    """Datos del panel médico en una pasada: la cola de espera ACOTADA a las colas de este
    médico (todas sus especialidades), sus consultas abiertas, cuántas ha cerrado y el alcance de
    su cola (los grupos que pinta como cards, y el motivo si no ve ninguna).

    El filtro por especialidad se aplica AQUÍ, en SQL, con la regla de `queue_access`, y
    `claim_consultation` la revalida: el filtro de una lista nunca es un control de acceso por sí
    solo.

    La cola son los casos `waiting` sin asignar, por orden de llegada del paciente (`queued_at`):
    un caso derivado conserva la hora a la que llegó, no la de la derivación."""
    scope = await queue_access.queue_scope(
        session, user_id=doctor_user_id, specialty_id=doctor_specialty_id, is_admin=is_admin
    )
    waiting_stmt = _with_specialty_names(
        select(Consultation)
        .where(
            Consultation.assigned_doctor_id.is_(None),
            Consultation.status == "waiting",
            scope.sql_filter(),
        )
        .order_by(Consultation.queued_at.asc(), Consultation.created_at.asc(), Consultation.id)
    )
    mine_stmt = _with_specialty_names(
        select(Consultation)
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
    return waiting, mine, my_closed, scope


# --- Derivación a la cola de otra especialidad ---


async def derivation_targets(session: AsyncSession) -> list[Specialty]:
    """Especialidades a las que se puede derivar un paciente: activas, no de relleno y con al
    menos un médico habilitado mirando esa cola. Mandar un caso a una cola que nadie ve es dejar
    al paciente esperando para siempre."""
    stmt = (
        select(Specialty)
        .where(
            Specialty.deleted_at.is_(None),
            Specialty.status == "active",
            Specialty.is_placeholder.is_(False),
            practicing_doctor_exists(Specialty.id),
        )
        .order_by(Specialty.sort_order.asc(), Specialty.name.asc(), Specialty.id)
    )
    return list((await session.scalars(stmt)).all())


async def _derivation_target(
    session: AsyncSession, target_specialty_id: uuid.UUID, current_specialty_id: uuid.UUID | None
) -> Specialty:
    """Valida el destino de una derivación (422 con el motivo si no sirve)."""
    target = await session.get(Specialty, target_specialty_id)
    if (
        target is None
        or target.deleted_at is not None
        or target.status != "active"
        or target.is_placeholder
    ):
        raise UnprocessableError("La especialidad de destino no es válida.")
    if target.id == current_specialty_id:
        raise UnprocessableError("El caso ya está en la cola de esa especialidad.")
    has_doctor = await session.scalar(select(practicing_doctor_exists(target.id)))
    if not has_doctor:
        raise UnprocessableError("Esa especialidad todavía no tiene médicos que atiendan su cola.")
    return target


async def derive_in_queue(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    *,
    target_specialty_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_specialty_id: uuid.UUID | None,
    actor_is_admin: bool,
    principal: "Principal",
    ip: str | None = None,
) -> Consultation:
    """Deriva un caso que NADIE ha tomado a la cola de otra especialidad.

    Es el mismo caso (nadie lo atendió: no hay acto médico que conservar), cambia de cola y
    conserva `queued_at`, así que no pierde su turno. Solo lo deriva quien lo ve en su cola.

    Escritura condicional sobre la especialidad que se leyó: si en el medio otro médico lo tomó
    o lo derivó, el UPDATE no matchea y se responde 409 en vez de pisar su decisión.

    Derivar sin poder leer el motivo no tiene sentido (decisión de producto 2026-09-23): además
    de estar en su cola (`ensure_can_take`), el caller necesita el mismo permiso clínico que le
    daría el panel sobre ESTE caso (`clinical_access.grant_for_queue_item`) — así un admin que
    no ejerce, que sí ve todas las colas, no puede derivar un caso cuyo motivo no puede leer."""
    from src.services import clinical_access  # diferido: mismo ciclo que `Principal`

    consultation = await get_consultation(session, consultation_id)
    if consultation.status != "waiting" or consultation.assigned_doctor_id is not None:
        raise ConflictError("Este caso ya no está en la cola.")
    await queue_access.ensure_can_take(
        session,
        consultation,
        user_id=actor_user_id,
        specialty_id=actor_specialty_id,
        is_admin=actor_is_admin,
    )
    scope = await clinical_access.queue_grant(session, principal)
    grant = clinical_access.grant_for_queue_item(
        principal,
        scope,
        assigned_doctor_id=consultation.assigned_doctor_id,
        specialty_id=consultation.specialty_id,
        status=consultation.status,
    )
    if grant is None:
        await clinical_access.audit_clinical_denied(
            session,
            principal=principal,
            ip=ip,
            resource="consultations",
            resource_id=consultation.id,
        )
        raise ForbiddenError("Solo puede derivar quien puede ver el motivo del caso.")
    origin_id = consultation.specialty_id
    target = await _derivation_target(session, target_specialty_id, origin_id)
    result = await session.execute(
        update(Consultation)
        .where(
            Consultation.id == consultation_id,
            Consultation.status == "waiting",
            Consultation.assigned_doctor_id.is_(None),
            Consultation.specialty_id == origin_id,
        )
        .values(specialty_id=target.id, derived_from_specialty_id=origin_id)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        raise ConflictError("El caso cambió mientras lo derivabas: otro médico lo tomó o movió.")
    session.add(
        ConsultationEvent(
            consultation_id=consultation_id,
            event_type=DERIVED_EVENT,
            created_by=actor_user_id,
        )
    )
    await audit.log_action(
        session,
        action="consultation.derived",
        actor_user_id=actor_user_id,
        resource="consultations",
        resource_id=consultation_id,
        metadata={"from_specialty_id": str(origin_id), "to_specialty_id": str(target.id)},
    )
    await session.commit()
    await session.refresh(consultation)
    return consultation


async def refer_to_queue(
    session: AsyncSession,
    parent_id: uuid.UUID,
    *,
    target_specialty_id: uuid.UUID,
    reason: str,
    signature: str | None,
    actor_user_id: uuid.UUID,
    actor_is_admin: bool,
    actor_practices: bool = False,
) -> Consultation:
    """Derivar con especialista desde un caso YA atendido: el médico cierra su parte (firmada) y
    el paciente entra a la cola de la especialidad destino, sin cita.

    - Padre: `referred_to_specialist`, `closed_at`, firma y evento con el motivo. Sale de "Mis
      pacientes" del médico.
    - Hija: nueva consulta de la cadena en `waiting`, sin médico ni sala (la sala se crea al
      tomarla), con la especialidad destino y el `queued_at` del padre (conserva su turno).
      Evento `derived` con el motivo: es lo que ve el especialista.

    El cierre del padre es condicional (sigue abierto y con el mismo médico): si en el medio un
    admin lo cerró o lo reasignó, 409 en vez de derivar un caso que ya no es de quien deriva.
    El motivo es clínico: solo deriva el médico tratante."""
    parent = await get_consultation(session, parent_id)
    if parent.assigned_doctor_id is None or parent.status not in _OPEN_ASSIGNED_STATUSES:
        raise ConflictError("Solo se deriva con especialista un caso que se está atendiendo.")
    _ensure_can_manage(parent, actor_user_id, actor_is_admin)
    _ensure_can_write_clinical(parent, actor_user_id, actor_practices)
    target = await _derivation_target(session, target_specialty_id, parent.specialty_id)

    now = datetime.now(UTC)
    values: dict = {"status": "referred_to_specialist", "closed_at": now}
    if signature is not None:
        values["close_signature"] = signature
    result = await session.execute(
        update(Consultation)
        .where(
            Consultation.id == parent.id,
            Consultation.status.in_(_OPEN_ASSIGNED_STATUSES),
            Consultation.assigned_doctor_id == parent.assigned_doctor_id,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        raise ConflictError("La consulta cambió mientras la derivabas. Recarga la página.")
    session.add(
        ConsultationEvent(
            consultation_id=parent.id,
            event_type="referred_to_specialist",
            created_by=actor_user_id,
            note=reason,
        )
    )
    child = Consultation(
        patient_id=parent.patient_id,
        specialty_id=target.id,
        derived_from_specialty_id=parent.specialty_id,
        chief_complaint=parent.chief_complaint,  # `Sealed` tal cual (misma columna)
        category=parent.category,
        priority=parent.priority,
        status="waiting",
        parent_consultation_id=parent.id,
        queued_at=parent.queued_at,
    )
    session.add(child)
    await session.flush()
    session.add(
        ConsultationEvent(
            consultation_id=child.id,
            event_type=DERIVED_EVENT,
            created_by=actor_user_id,
            note=reason,
        )
    )
    await audit.log_action(
        session,
        action="consultation.referred_to_queue",
        actor_user_id=actor_user_id,
        resource="consultations",
        resource_id=child.id,
        metadata={
            "parent_id": str(parent.id),
            "from_specialty_id": str(parent.specialty_id),
            "to_specialty_id": str(target.id),
        },
    )
    await session.commit()
    await session.refresh(child)
    return child


@dataclass(frozen=True)
class Derivation:
    """Quién derivó el caso a su cola actual, desde qué especialidad y por qué."""

    from_specialty: str | None
    by_name: str | None
    reason: Sealed | str | None  # nota clínica: se revela solo en el esquema, con permiso
    at: datetime


async def get_derivation(session: AsyncSession, consultation: Consultation) -> Derivation | None:
    """La derivación más reciente del caso (None si nunca se derivó). El motivo y el autor salen
    del evento `derived`: es la única copia del motivo."""
    if consultation.derived_from_specialty_id is None:
        return None
    row = (
        await session.execute(
            select(ConsultationEvent.note, ConsultationEvent.created_at, Profile.full_name)
            .outerjoin(Profile, Profile.id == ConsultationEvent.created_by)
            .where(
                ConsultationEvent.consultation_id == consultation.id,
                ConsultationEvent.event_type == DERIVED_EVENT,
            )
            .order_by(ConsultationEvent.created_at.desc(), ConsultationEvent.id.desc())
            .limit(1)
        )
    ).first()
    from_name = await session.scalar(
        select(Specialty.name).where(Specialty.id == consultation.derived_from_specialty_id)
    )
    return Derivation(
        from_specialty=from_name,
        by_name=row.full_name if row else None,
        reason=row.note if row else None,
        at=row.created_at if row else consultation.created_at,
    )


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
    """Genera (idempotente) la sala Jitsi de la consulta. Si ya existe, la devuelve; si no, la
    crea mientras el caso siga abierto (en espera o en atención).

    En atención también: la atención es siempre por video, y hay casos tomados sin sala (los que
    se tomaron por WhatsApp o cuando fallaba la creación previa al claim). Su médico tiene que
    poder abrirla desde el detalle.

    Escritura condicional (`video_room_url IS NULL`): dos llamadas simultáneas no pueden dejar a
    médico y paciente en salas distintas; la segunda relee la que ganó."""
    consultation = await get_consultation(session, consultation_id)
    if consultation.video_room_url:
        return consultation
    if consultation.status not in _ROOM_STATUSES:
        raise ConflictError("La consulta ya no está abierta.")
    await session.execute(
        update(Consultation)
        .where(
            Consultation.id == consultation_id,
            Consultation.video_room_url.is_(None),
            Consultation.status.in_(_ROOM_STATUSES),
        )
        .values(video_room_url=new_room_url())
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.refresh(consultation)
    if not consultation.video_room_url:
        raise ConflictError("La consulta ya no está abierta.")
    return consultation


async def update_consultation(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    data: ConsultationUpdate,
    actor_user_id: uuid.UUID | None = None,
    actor_is_admin: bool = False,
    actor_practices: bool = False,
) -> Consultation:
    """PATCH de la consulta. `actor_practices` = el actor ejerce como médico habilitado; con
    eso y la asignación se decide si puede escribir motivo/notas (`_ensure_can_write_clinical`):
    el admin cambia estado, prioridad, asignación, `nota_admin` y `admin_seguimiento`, pero un
    campo clínico en su PATCH es 403, y asignarse el caso a sí mismo también."""
    _validate_status(data.status)
    consultation = await get_consultation(session, consultation_id)
    _ensure_can_manage(consultation, actor_user_id, actor_is_admin)
    changes = data.model_dump(exclude_unset=True)
    if changes.keys() & _CLINICAL_UPDATE_FIELDS:
        _ensure_can_write_clinical(consultation, actor_user_id, actor_practices)
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
    # Nadie se asigna un caso a sí mismo por PATCH, tampoco el admin: un admin que además ejerce
    # quedaría como médico tratante (acceso clínico completo) de cualquier caso, incluso de uno
    # que atiende otro médico. Asignar a OTRO médico sí es gestión admin; tomarlo para sí, solo
    # por el claim atómico (POST /{id}/claim o /queue/{id}/take).
    if (
        "assigned_doctor_id" in changes
        and changes["assigned_doctor_id"] is not None
        and changes["assigned_doctor_id"] == actor_user_id
        and consultation.assigned_doctor_id != actor_user_id
    ):
        raise ForbiddenError(
            "No puedes asignarte un caso a ti mismo: tómalo desde la cola (claim atómico)."
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
    actor_practices: bool = False,
) -> ConsultationEvent:
    consultation = await get_consultation(session, consultation_id)  # 404 si no existe
    # Mismo anti-IDOR que update/close: los eventos son el historial/auditoría del caso;
    # sin este check un médico podría inyectar un evento falso (p.ej. "closed") en la
    # consulta de otro.
    _ensure_can_manage(consultation, created_by, actor_is_admin)
    if data.consultation_id != consultation_id:
        raise BadRequestError("El consultation_id del cuerpo no coincide con el de la ruta.")
    # La nota de un evento es texto clínico: solo la escribe el médico tratante. Excepción:
    # `admin_update`, la traza que deja el panel al cambiar estado/médico/especialidad ("Estado:
    # Cerrada (Ana)"). Sin ella, el PATCH del admin entraba pero el evento daba 403 y el panel
    # decía "No se pudo actualizar el caso" (regresión del 2026-09-23). La nota se sigue guardando
    # cifrada y leyéndose como nota clínica.
    if data.note is not None and data.event_type != ADMIN_UPDATE_EVENT:
        _ensure_can_write_clinical(consultation, created_by, actor_practices)
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
