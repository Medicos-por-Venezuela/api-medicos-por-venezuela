"""Servicio de mensajería (médico ↔ paciente).

Reglas duras (.claude/rules/mensajeria.md y security.md):
- Un hilo = una consulta (messages.consultation_id).
- Cuerpos cifrados en reposo (EncryptedText).
- Grant de lectura: solo médico tratante/cadena o paciente dueño. El admin NO lee cuerpos.
- Auditoría clínica: toda lectura concedida registra READ_CLINICAL_DATA.
- Presencia asimétrica: solo el médico tratante ve si el paciente está en línea.
- Adjuntos clínicos: PDF e imágenes (JPG, PNG, WEBP). GIF estrictamente prohibido (422).
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core import consultation_token
from src.core.clinical_crypto import reveal
from src.core.config import settings
from src.core.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnprocessableError,
)
from src.core.observability import correlation_id_ctx
from src.core.security import Principal
from src.models.clinical import Message, MessageAttachment
from src.models.consultation import Consultation
from src.models.consultation_event import ConsultationEvent
from src.models.patient import Patient
from src.models.specialty import Specialty
from src.schemas.clinical import ClinicalGrant, summary_grant, treating_grant
from src.schemas.message import InboxThreadResponse, MessageCreate
from src.services import notifications, storage
from src.services.audit import log_action
from src.services.clinical_access import audit_clinical_read

logger = logging.getLogger("mpv.messaging")

# Registro en memoria de presencia de pacientes (D3 en spec-chat-tiempo-real.md)
_ACTIVE_PATIENTS: dict[uuid.UUID, datetime] = {}


def record_patient_presence(consultation_id: uuid.UUID) -> None:
    """Registra actividad reciente del paciente para esta consulta."""
    _ACTIVE_PATIENTS[consultation_id] = datetime.now(UTC)


def is_patient_online(consultation_id: uuid.UUID, last_seen_at: datetime | None = None) -> bool:
    """Determina si el paciente está en línea (actividad en últimos 45 segundos)."""
    now = datetime.now(UTC)
    active_ts = _ACTIVE_PATIENTS.get(consultation_id)
    if active_ts:
        if (now - active_ts).total_seconds() <= 45:
            return True
        _ACTIVE_PATIENTS.pop(consultation_id, None)

    if last_seen_at:
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=UTC)
        if (now - last_seen_at).total_seconds() <= 45:
            return True

    return False


async def is_doctor_in_chain(
    session: AsyncSession, consultation: Consultation, doctor_user_id: uuid.UUID
) -> bool:
    """Verifica si el médico es el tratante actual o previo en la cadena."""
    if consultation.assigned_doctor_id == doctor_user_id:
        return True

    # Eventos de asignación en esta consulta
    stmt = (
        select(ConsultationEvent.id)
        .where(
            ConsultationEvent.consultation_id == consultation.id,
            ConsultationEvent.created_by == doctor_user_id,
        )
        .limit(1)
    )
    if (await session.scalar(stmt)) is not None:
        return True

    # Recorrer ancestros (parent_consultation_id)
    curr = consultation
    seen = {curr.id}
    while curr.parent_consultation_id is not None and curr.parent_consultation_id not in seen:
        seen.add(curr.parent_consultation_id)
        parent = await session.get(Consultation, curr.parent_consultation_id)
        if parent is None:
            break
        if parent.assigned_doctor_id == doctor_user_id:
            return True
        curr = parent

    return False


async def is_patient_owner(
    session: AsyncSession,
    consultation: Consultation,
    principal: Principal | None,
    consultation_token_str: str | None,
) -> bool:
    """Verifica pertenencia del paciente a la consulta (por token o por cuenta)."""
    if consultation_token.is_valid_for(consultation_token_str, consultation.id):
        return True
    if principal is not None and not principal.is_staff:
        patient = await session.get(Patient, consultation.patient_id)
        if patient is not None and patient.user_id == principal.id:
            return True
    return False


def check_can_write_in_consultation(consultation: Consultation, is_doctor: bool) -> None:
    """Valida si el estado y ventana de la consulta admiten nuevos mensajes."""
    if is_doctor and consultation.status == "waiting":
        raise ConflictError("La consulta aún no ha sido tomada por un médico.")

    _CLOSED_STATUSES = {"closed", "cancelled", "patient_no_show", "closed_by_admin"}
    if consultation.status in _CLOSED_STATUSES:
        closed_reference = (
            consultation.closed_at or consultation.ended_at or consultation.created_at
        )
        if closed_reference:
            if closed_reference.tzinfo is None:
                closed_reference = closed_reference.replace(tzinfo=UTC)
            limit = closed_reference + timedelta(hours=settings.MESSAGING_AFTER_CLOSE_HOURS)
            if datetime.now(UTC) > limit:
                raise ConflictError("Esta consulta ya no admite mensajes.")


def validate_attachment_file(
    filename: str, content: bytes, content_type: str | None
) -> tuple[str, str]:
    """Valida extensión, magic bytes y tamaño del archivo.

    Rechaza estrictamente GIF con UnprocessableError (HTTP 422).
    Devuelve (mime_type_validado, clean_filename).
    """
    if not filename:
        raise UnprocessableError("El nombre del archivo es requerido.")
    clean_name = Path(filename).name.strip()
    if not clean_name:
        raise UnprocessableError("Nombre de archivo inválido.")

    # 1. Prohibición estricta de GIF (R15.2)
    lower_name = clean_name.lower()
    if lower_name.endswith(".gif") or (content_type and content_type.lower() == "image/gif"):
        raise UnprocessableError("El formato GIF no está permitido.")
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        raise UnprocessableError("El formato GIF no está permitido.")

    # 2. Tamaño
    if len(content) == 0:
        raise UnprocessableError("El archivo no puede estar vacío.")
    if len(content) > settings.MESSAGING_MAX_ATTACHMENT_SIZE_BYTES:
        raise UnprocessableError(
            f"El archivo excede el tamaño máximo permitido de "
            f"{settings.MESSAGING_MAX_ATTACHMENT_SIZE_BYTES // (1024 * 1024)} MB."
        )

    # 3. Magic bytes
    detected_mime: str | None = None
    if content.startswith(b"%PDF"):
        detected_mime = "application/pdf"
    elif content.startswith(b"\xff\xd8\xff"):
        detected_mime = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_mime = "image/png"
    elif content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        detected_mime = "image/webp"

    if detected_mime is None or detected_mime not in settings.messaging_allowed_mime_types:
        raise UnprocessableError("Formato de archivo no admitido o contenido no válido.")

    return detected_mime, clean_name


async def upload_attachment(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    filename: str,
    content: bytes,
    content_type: str | None,
    principal: Principal | None,
    consultation_token_str: str | None,
    client_ip: str | None = None,
) -> tuple[MessageAttachment, ClinicalGrant]:
    """Sube un archivo adjunto, valida magic bytes y lo persiste cifrado."""
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    # Permisos de subida: tratante o paciente dueño
    if principal is not None and principal.is_staff:
        if not await is_doctor_in_chain(session, consultation, principal.id):
            raise NotFoundError("Consulta no encontrada.")
        uploader_role = "doctor"
        uploader_user_id = principal.id
        grant = treating_grant("assigned_doctor")
    else:
        if not await is_patient_owner(session, consultation, principal, consultation_token_str):
            raise NotFoundError("Consulta no encontrada.")
        uploader_role = "patient"
        uploader_user_id = principal.id if (principal and not principal.is_staff) else None
        grant = summary_grant("patient_owner")
        record_patient_presence(consultation.id)
        consultation.patient_last_seen_at = datetime.now(UTC)

    check_can_write_in_consultation(consultation, is_doctor=(uploader_role == "doctor"))

    mime_type, clean_name = validate_attachment_file(filename, content, content_type)

    attachment_id = uuid.uuid4()
    storage_path = f"consultations/{consultation.id}/attachments/{attachment_id}.bin"
    storage.save_attachment_file(storage_path, content)

    attachment = MessageAttachment(
        id=attachment_id,
        consultation_id=consultation.id,
        message_id=None,
        uploader_role=uploader_role,
        uploader_user_id=uploader_user_id,
        file_name=clean_name,
        mime_type=mime_type,
        file_size_bytes=len(content),
        storage_path=storage_path,
        created_at=datetime.now(UTC),
    )
    session.add(attachment)

    await log_action(
        session,
        action="attachment.uploaded",
        actor_user_id=uploader_user_id,
        resource="message_attachments",
        resource_id=str(attachment.id),
        metadata={
            "consultation_id": str(consultation.id),
            "mime_type": mime_type,
            "size": len(content),
        },
        ip=client_ip,
        correlation_id=correlation_id_ctx.get(),
    )
    await session.commit()
    await session.refresh(attachment)
    return attachment, grant


async def get_attachment_for_download(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    attachment_id: uuid.UUID,
    principal: Principal | None,
    consultation_token_str: str | None,
    client_ip: str | None = None,
) -> tuple[bytes, str, str]:
    """Valida grant clínico y devuelve el binario, nombre de archivo y tipo MIME."""
    attachment = await session.get(MessageAttachment, attachment_id)
    if attachment is None or attachment.consultation_id != consultation_id:
        raise NotFoundError("Adjunto no encontrado.")

    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    # Evaluación de grant clínico (admin NO puede descargar archivos clínicos)
    grant: ClinicalGrant | None = None
    if principal is not None and principal.is_staff:
        if await is_doctor_in_chain(session, consultation, principal.id):
            grant = treating_grant("assigned_doctor")
        elif principal.is_admin:
            # Ser admin sin ser el médico tratante no concede acceso a adjuntos clínicos
            raise ForbiddenError("Los administradores no tienen acceso a adjuntos clínicos.")
        else:
            raise NotFoundError("Consulta no encontrada.")
    elif await is_patient_owner(session, consultation, principal, consultation_token_str):
        grant = summary_grant("patient_owner")
        record_patient_presence(consultation.id)
    else:
        raise NotFoundError("Consulta no encontrada.")

    data = storage.get_attachment_file(attachment.storage_path)
    if data is None:
        raise NotFoundError("El archivo no se encuentra en el almacenamiento.")

    clean_filename = reveal(attachment.file_name) or "archivo"

    await audit_clinical_read(
        session,
        principal=principal,
        ip=client_ip,
        resource="message_attachments",
        grants=[(attachment.id, grant)],
    )

    return data, clean_filename, attachment.mime_type


async def send_message(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    data: MessageCreate,
    principal: Principal | None,
    consultation_token_str: str | None,
    client_ip: str | None = None,
) -> tuple[Message, ClinicalGrant | None, bool]:
    """Envía un mensaje en el hilo de una consulta.

    Devuelve (Message, grant, created: bool). Si ya existe por client_msg_id, created es False.
    """
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    is_doctor = principal is not None and principal.is_staff
    if is_doctor:
        if not await is_doctor_in_chain(session, consultation, principal.id):
            raise NotFoundError("Consulta no encontrada.")
        sender_role = "doctor"
        direction = "doctor_to_patient"
        sender_user_id = principal.id
        grant = treating_grant("assigned_doctor")
    else:
        if not await is_patient_owner(session, consultation, principal, consultation_token_str):
            raise NotFoundError("Consulta no encontrada.")
        sender_role = "patient"
        direction = "patient_to_doctor"
        sender_user_id = principal.id if (principal and not principal.is_staff) else None
        grant = summary_grant("patient_owner")
        record_patient_presence(consultation.id)
        consultation.patient_last_seen_at = datetime.now(UTC)

    check_can_write_in_consultation(consultation, is_doctor=is_doctor)

    # Límite de mensajes por hora para pacientes (CA4.4)
    if sender_role == "patient":
        one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
        patient_hourly_count = await session.scalar(
            select(func.count(Message.id)).where(
                Message.consultation_id == consultation.id,
                Message.sender_role == "patient",
                Message.sent_at >= one_hour_ago,
            )
        )
        if (
            patient_hourly_count is not None
            and patient_hourly_count >= settings.MESSAGING_PATIENT_HOURLY_LIMIT
        ):
            raise ConflictError("Límite de mensajes por hora alcanzado para esta consulta.")

    # Idempotencia por client_msg_id
    if data.client_msg_id:
        existing = await session.scalar(
            select(Message)
            .where(
                Message.consultation_id == consultation.id,
                Message.client_msg_id == data.client_msg_id,
            )
            .options(selectinload(Message.attachments))
        )
        if existing is not None:
            return existing, grant, False

    # Validar y vincular adjuntos
    attachments: list[MessageAttachment] = []
    if data.attachment_ids:
        stmt = select(MessageAttachment).where(
            MessageAttachment.id.in_(data.attachment_ids),
            MessageAttachment.consultation_id == consultation.id,
        )
        attachments = list((await session.execute(stmt)).scalars().all())
        if len(attachments) != len(data.attachment_ids):
            raise BadRequestError("Uno o más adjuntos no existen o no pertenecen a esta consulta.")

    kind = "attachment" if (not data.body and data.attachment_ids) else "text"

    message = Message(
        id=uuid.uuid4(),
        consultation_id=consultation.id,
        sender_role=sender_role,
        sender_user_id=sender_user_id,
        direction=direction,
        channel="web",
        kind=kind,
        body=data.body,
        client_msg_id=data.client_msg_id,
        sent_at=datetime.now(UTC),
        delivery_status="sent",
    )
    session.add(message)
    await session.flush()

    for att in attachments:
        att.message_id = message.id

    await log_action(
        session,
        action="message.sent",
        actor_user_id=sender_user_id,
        resource="messages",
        resource_id=str(message.id),
        metadata={
            "consultation_id": str(consultation.id),
            "direction": direction,
            "has_attachments": bool(data.attachment_ids),
        },
        ip=client_ip,
        correlation_id=correlation_id_ctx.get(),
    )
    await session.commit()

    # Recargar con adjuntos poblados
    reloaded = await session.scalar(
        select(Message).where(Message.id == message.id).options(selectinload(Message.attachments))
    )
    return reloaded or message, grant, True


async def list_messages(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    principal: Principal | None,
    consultation_token_str: str | None,
    limit: int = 50,
    offset: int = 0,
    after_id: uuid.UUID | None = None,
    before_id: uuid.UUID | None = None,
    client_ip: str | None = None,
) -> tuple[list[Message], int, ClinicalGrant | None]:
    """Lista los mensajes de una consulta con grant clínico.

    Devuelve (messages, unread_count, grant).
    """
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    grant: ClinicalGrant | None = None
    is_doctor = principal is not None and principal.is_staff
    if is_doctor:
        if await is_doctor_in_chain(session, consultation, principal.id):
            grant = treating_grant("assigned_doctor")
        elif principal.is_admin:
            # Admin sin ser médico del caso ve metadatos pero NO cuerpos (grant = None)
            grant = None
        else:
            raise NotFoundError("Consulta no encontrada.")
    elif await is_patient_owner(session, consultation, principal, consultation_token_str):
        grant = summary_grant("patient_owner")
        record_patient_presence(consultation.id)
    else:
        raise NotFoundError("Consulta no encontrada.")

    # Dirección contraria para marcar entrega y contar no leídos
    opposing_direction = "patient_to_doctor" if is_doctor else "doctor_to_patient"

    # Marcar delivered_at en mensajes contrarios
    if is_doctor or (grant is not None):
        await session.execute(
            update(Message)
            .where(
                Message.consultation_id == consultation.id,
                Message.direction == opposing_direction,
                Message.delivered_at.is_(None),
            )
            .values(delivered_at=func.now())
        )

    # Conteo de no leídos para el llamante
    unread_count = (
        await session.scalar(
            select(func.count(Message.id)).where(
                Message.consultation_id == consultation.id,
                Message.direction == opposing_direction,
                Message.read_at.is_(None),
            )
        )
        or 0
    )

    # Consulta de mensajes
    stmt = (
        select(Message)
        .where(Message.consultation_id == consultation.id)
        .order_by(Message.sent_at.asc(), Message.id.asc())
        .options(selectinload(Message.attachments))
    )

    if after_id:
        after_msg = await session.get(Message, after_id)
        if after_msg:
            stmt = stmt.where(
                (Message.sent_at > after_msg.sent_at)
                | ((Message.sent_at == after_msg.sent_at) & (Message.id > after_msg.id))
            )
    if before_id:
        before_msg = await session.get(Message, before_id)
        if before_msg:
            stmt = stmt.where(
                (Message.sent_at < before_msg.sent_at)
                | ((Message.sent_at == before_msg.sent_at) & (Message.id < before_msg.id))
            )

    stmt = stmt.limit(min(limit, 100)).offset(offset)
    result = await session.execute(stmt)
    messages = list(result.scalars().all())

    # Auditoría clínica si hubo grant
    if grant is not None:
        await audit_clinical_read(
            session,
            principal=principal,
            ip=client_ip,
            resource="messages",
            grants=[(consultation.id, grant)],
        )

    await session.commit()
    return messages, unread_count, grant


async def mark_as_read(
    session: AsyncSession,
    consultation_id: uuid.UUID,
    principal: Principal | None,
    consultation_token_str: str | None,
) -> int:
    """Marca como leídos los mensajes no leídos de la otra dirección (idempotente)."""
    consultation = await session.get(Consultation, consultation_id)
    if consultation is None:
        raise NotFoundError("Consulta no encontrada.")

    is_doctor = principal is not None and principal.is_staff
    if is_doctor:
        if await is_doctor_in_chain(session, consultation, principal.id):
            opposing_direction = "patient_to_doctor"
        elif principal.is_admin:
            raise ForbiddenError("Los administradores no marcan mensajes como leídos.")
        else:
            raise NotFoundError("Consulta no encontrada.")
    elif await is_patient_owner(session, consultation, principal, consultation_token_str):
        opposing_direction = "doctor_to_patient"
        record_patient_presence(consultation.id)
    else:
        raise NotFoundError("Consulta no encontrada.")

    stmt = (
        update(Message)
        .where(
            Message.consultation_id == consultation.id,
            Message.direction == opposing_direction,
            Message.read_at.is_(None),
        )
        .values(read_at=func.now())
    )
    result = await session.execute(stmt)
    marked = result.rowcount
    await session.commit()

    reader_role = "doctor" if is_doctor else "patient"
    notifications.clear_mail_debounce(consultation.id, reader_role)

    return marked


async def get_inbox(
    session: AsyncSession,
    principal: Principal,
    only_unread: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[InboxThreadResponse], int]:
    """Lista las conversaciones activas para el buzón del médico con presencia asimétrica."""
    unread_filter = (Message.direction == "patient_to_doctor") & Message.read_at.is_(None)

    stmt = (
        select(
            Consultation.id,
            Consultation.code,
            Consultation.status,
            Consultation.patient_last_seen_at,
            Patient.full_name.label("patient_name"),
            Specialty.name.label("specialty"),
            func.max(Message.sent_at).label("last_message_at"),
            func.count(case((unread_filter, 1))).label("unread_count"),
        )
        .join(Patient, Patient.id == Consultation.patient_id)
        .outerjoin(Specialty, Specialty.id == Consultation.specialty_id)
        .join(Message, Message.consultation_id == Consultation.id)
    )

    if not principal.is_admin:
        stmt = stmt.where(
            (Consultation.assigned_doctor_id == principal.id)
            | (
                Consultation.parent_consultation_id.in_(
                    select(Consultation.id).where(Consultation.assigned_doctor_id == principal.id)
                )
            )
        )

    stmt = stmt.group_by(
        Consultation.id,
        Consultation.code,
        Consultation.status,
        Consultation.patient_last_seen_at,
        Patient.full_name,
        Specialty.name,
    )

    if only_unread:
        stmt = stmt.having(func.count(case((unread_filter, 1))) > 0)

    # Subquery para total
    subq = stmt.subquery()
    total = (await session.execute(select(func.count()).select_from(subq))).scalar() or 0

    stmt = (
        stmt.order_by(func.max(Message.sent_at).desc(), Consultation.id.desc())
        .offset(offset)
        .limit(min(limit, 100))
    )
    rows = (await session.execute(stmt)).all()

    items: list[InboxThreadResponse] = []
    for row in rows:
        cid = row.id
        latest_msg = await session.scalar(
            select(Message)
            .where(Message.consultation_id == cid)
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(1)
        )
        last_direction = latest_msg.direction if latest_msg else "patient_to_doctor"
        patient_online = is_patient_online(cid, row.patient_last_seen_at)

        items.append(
            InboxThreadResponse(
                consultation_id=cid,
                code=row.code,
                specialty=row.specialty,
                patient_name=row.patient_name,
                status=row.status,
                last_message_at=row.last_message_at,
                last_direction=last_direction,
                unread_count=row.unread_count,
                patient_online=patient_online,
                patient_last_seen_at=row.patient_last_seen_at,
                active_call=None,
            )
        )

    return items, total


async def get_latest_patient_message_signal(
    session: AsyncSession, consultation_id: uuid.UUID
) -> tuple[uuid.UUID, str, datetime, int] | None:
    """Obtiene el último mensaje dirigido al paciente para el stream SSE de la sala (R8.1).

    Devuelve (message_id, direction, sent_at, unread_count) o None si no hay mensajes.
    """
    stmt = (
        select(Message.id, Message.direction, Message.sent_at)
        .where(
            Message.consultation_id == consultation_id,
            Message.direction == "doctor_to_patient",
        )
        .order_by(Message.sent_at.desc(), Message.id.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None

    unread_count = (
        await session.scalar(
            select(func.count(Message.id)).where(
                Message.consultation_id == consultation_id,
                Message.direction == "doctor_to_patient",
                Message.read_at.is_(None),
            )
        )
    ) or 0
    return (row[0], row[1], row[2], unread_count)


async def get_inbox_signal(
    session: AsyncSession, principal: Principal
) -> tuple[int, dict[uuid.UUID, tuple[datetime | None, int]]]:
    """Obtiene total de no leídos y mapa de hilos para el stream SSE del buzón del médico (R8.2).

    Devuelve (total_unread, {consultation_id: (last_message_at, unread_count)}).
    """
    unread_filter = (Message.direction == "patient_to_doctor") & Message.read_at.is_(None)

    stmt = select(
        Consultation.id,
        func.max(Message.sent_at).label("last_message_at"),
        func.count(case((unread_filter, 1))).label("unread_count"),
    ).join(Message, Message.consultation_id == Consultation.id)

    if not principal.is_admin:
        stmt = stmt.where(
            (Consultation.assigned_doctor_id == principal.id)
            | (
                Consultation.parent_consultation_id.in_(
                    select(Consultation.id).where(Consultation.assigned_doctor_id == principal.id)
                )
            )
        )

    stmt = stmt.group_by(Consultation.id)
    rows = (await session.execute(stmt)).all()

    threads: dict[uuid.UUID, tuple[datetime | None, int]] = {}
    unread_total = 0
    for r in rows:
        threads[r.id] = (r.last_message_at, r.unread_count)
        unread_total += r.unread_count

    return unread_total, threads
