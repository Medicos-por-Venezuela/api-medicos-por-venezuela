"""Capa HTTP (delgada) para consultations y sus eventos.

Autorización (replica las RLS):
- Crear consulta: sin sesión (auto-servicio del paciente anónimo), con rate limit.
- Sala de video / entered-call: sin sesión pero con el token de acceso de ESA consulta.
- Leer: staff ve todo; un paciente autenticado solo ve lo suyo (anti-IDOR).
- Actualizar / cerrar / eventos: staff. Eliminar: admin.
"""

import logging
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import consultation_token
from src.core.config import settings
from src.core.ratelimit import limiter
from src.core.security import (
    Principal,
    get_current_principal,
    get_optional_principal,
    require_permission,
)
from src.db.session import get_db
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.schemas.consultation import (
    ChainItem,
    ConsultationClaimRequest,
    ConsultationCloseRequest,
    ConsultationCreate,
    ConsultationCreatedResponse,
    ConsultationDetailResponse,
    ConsultationPanelResponse,
    ConsultationPatientResponse,
    ConsultationResponse,
    ConsultationUpdate,
    PanelConsultationItem,
    PanelWaitingItem,
    ReminderRunResponse,
    ScheduleFollowUpRequest,
    ScheduleReferralRequest,
)
from src.schemas.consultation_event import (
    ConsultationEventCreate,
    ConsultationEventResponse,
)
from src.services import consultations as consultations_service
from src.services import notifications

logger = logging.getLogger("mpv.api")

router = APIRouter(prefix="/consultations", tags=["consultations"])
tag_metadata = [
    {
        "name": "consultations",
        "description": "Casos/consultas y su historial de eventos (auditoría).",
    }
]

_NOT_FOUND = {404: {"description": "Consulta no encontrada."}}
_TOKEN_RESPONSES = {
    401: {"description": "Falta el token de acceso a la sala o no es válido para esta consulta."},
    429: {"description": "Demasiadas peticiones desde esta IP (rate limit)."},
}

# Cabecera y no query param: en la cabecera el token no queda en los logs del servidor ni en el
# `Referer`. Sigue viajando en la URL hasta el frontend (el paciente llega por link), pero de
# ahí al backend ya no.
_CONSULTATION_TOKEN_HEADER = "X-Consultation-Token"


async def _queue_appointment_email(
    background_tasks: BackgroundTasks, db: AsyncSession, child: Consultation
) -> None:
    """Encola el email "cita agendada" al paciente. Best-effort y fuera de la request: si el
    paciente no tiene email, `appointment_email_args` devuelve None y no se encola nada."""
    args = await notifications.appointment_email_args(db, child)
    if args:
        background_tasks.add_task(notifications.send_appointment_email, **args)


async def require_consultation_token(
    consultation_id: uuid.UUID,
    x_consultation_token: str | None = Header(default=None, alias=_CONSULTATION_TOKEN_HEADER),
    principal: Principal | None = Depends(get_optional_principal),
) -> None:
    """Exige el token de sala de ESTA consulta (hallazgo M3) **o** una sesión de staff.

    Estos endpoints los usan DOS clientes: el paciente anónimo, que llega por link y solo tiene
    el token, y el médico desde el panel, que tiene sesión pero NO el token del paciente (ver
    panel-medico.tsx: crea la sala si el caso llegó sin ella). Exigir solo el token dejaba al
    médico fuera de la consulta que está atendiendo.

    401 y no 403: el llamante es anónimo por diseño, no es que le falten permisos."""
    if principal is not None and principal.is_staff:
        return
    if consultation_token.is_valid_for(x_consultation_token, consultation_id):
        return
    logger.warning("SEC:consultation_token_invalid consultation_id=%s", consultation_id)
    if not settings.CONSULTATION_TOKEN_REQUIRED:
        # Ventana de cutover: se loguea pero se deja pasar, para no dejar sin sala al frontend
        # viejo mientras se despliegan backend y frontend por separado. Ver la nota en config.
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token de acceso a la consulta ausente, expirado o de otra consulta.",
    )


@router.get(
    "",
    response_model=list[ConsultationDetailResponse] | list[ConsultationPatientResponse],
    summary="Listar consultas",
)
async def list_consultations(
    skip: int = Query(0, ge=0),
    # Cap 200: el monitor admin/pacientes muestra los casos recientes (hasta 200) y filtra/ordena
    # en el cliente. Endpoint solo-staff; el default sigue en 100.
    limit: int = Query(100, ge=1, le=200),
    status_filter: str | None = Query(None, alias="status"),
    patient_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[ConsultationDetailResponse] | list[ConsultationPatientResponse]:
    """Staff ve todas las consultas con vista completa + el paciente anidado (para que el panel
    admin/pacientes no lea `patients` directo). Un paciente autenticado solo ve las suyas, sin
    notas clínicas ni internas ni datos anidados de otros."""
    consultations = await consultations_service.list_consultations(
        db,
        skip=skip,
        limit=limit,
        status=status_filter,
        patient_id=patient_id,
        viewer_is_staff=principal.is_staff,
        viewer_user_id=principal.id,
    )
    if principal.is_staff:
        return [ConsultationDetailResponse.model_validate(c) for c in consultations]
    return [ConsultationPatientResponse.model_validate(c) for c in consultations]


@router.post(
    "",
    response_model=ConsultationCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear consulta (público)",
    responses={
        400: {"description": "El `patient_id` no existe."},
        422: {"description": "`status` inválido."},
        429: {"description": "Demasiadas consultas desde esta IP (rate limit)."},
    },
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def create_consultation(
    request: Request, payload: ConsultationCreate, db: AsyncSession = Depends(get_db)
) -> ConsultationCreatedResponse:
    """Crea una consulta en espera. El `code` lo genera la base de datos (trigger).

    Devuelve además el `access_token` de la sala: es la ÚNICA vez que se entrega, porque el
    paciente anónimo no tiene sesión con la que volver a pedirlo. El frontend lo lleva en la
    URL de /sala-espera en lugar del id crudo.

    `request` es obligatorio para slowapi (lee la IP del cliente), aunque no se use aquí."""
    consultation = await consultations_service.create_consultation(db, payload)
    return ConsultationCreatedResponse(
        **ConsultationResponse.model_validate(consultation).model_dump(),
        access_token=consultation_token.issue(consultation.id),
    )


# NOTA: debe ir ANTES de "/{consultation_id}" o FastAPI intenta parsear "panel" como UUID (422).
@router.get(
    "/panel",
    response_model=ConsultationPanelResponse,
    summary="Cola del panel médico (espera + mías + cerradas)",
)
async def consultation_panel(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.read")),
) -> ConsultationPanelResponse:
    """Todo lo que el panel del médico necesita en una llamada: la cola de espera (casos sin
    asignar), las consultas abiertas del propio médico y cuántas ha cerrado. Reemplaza las
    lecturas directas a Supabase del panel."""
    waiting, mine, my_closed = await consultations_service.get_panel(
        db,
        principal.id,
        doctor_specialty_id=principal.specialty_id,
        is_admin=principal.is_admin,
    )
    return ConsultationPanelResponse(
        waiting=[PanelWaitingItem.model_validate(c) for c in waiting],
        mine=[PanelConsultationItem.model_validate(c) for c in mine],
        my_closed_count=my_closed,
    )


# NOTA: debe ir ANTES de "/{consultation_id}" o FastAPI intenta parsear "agenda" como UUID (422).
@router.get(
    "/agenda",
    response_model=list[ConsultationResponse],
    summary="Mi agenda: citas agendadas del médico autenticado",
)
async def my_agenda(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.read")),
) -> list[ConsultationResponse]:
    """Citas AGENDADAS (status 'scheduled') asignadas al médico autenticado, por fecha ascendente.
    El paciente ve las suyas por su propio scoping (list_consultations, viewer_is_staff=False)."""
    agenda = await consultations_service.list_agenda(db, doctor_user_id=principal.id)
    return [ConsultationResponse.model_validate(c) for c in agenda]


@router.post(
    "/agenda/send-due-reminders",
    response_model=ReminderRunResponse,
    summary="Enviar recordatorios de citas próximas (cron externo)",
)
async def send_due_reminders(
    window_minutes: int = Query(30, ge=1, le=1440, description="Ventana en minutos (def. 30)"),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("queue.manage")),
) -> ReminderRunResponse:
    """Envía el recordatorio de las citas agendadas cuya hora cae dentro de la ventana y que aún no
    lo recibieron (idempotente por `reminder_sent_at`). Pensado para un CRON externo que lo llame
    cada 1–5 min. Ver .knowledge/agenda.md."""
    sent = await notifications.send_due_reminders(db, window_minutes)
    return ReminderRunResponse(sent=sent, window_minutes=window_minutes)


@router.get(
    "/{consultation_id}",
    response_model=ConsultationDetailResponse | ConsultationPatientResponse,
    summary="Obtener consulta",
    responses=_NOT_FOUND,
)
async def get_consultation(
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ConsultationDetailResponse | ConsultationPatientResponse:
    """Staff recibe la vista completa (incluye notas clínicas/internas) + el paciente anidado, para
    que el panel no lea `patients` directo. Un paciente autenticado solo recibe su propia consulta
    sin las notas del médico."""
    consultation = await consultations_service.get_consultation(
        db, consultation_id, viewer_is_staff=principal.is_staff, viewer_user_id=principal.id
    )
    if principal.is_staff:
        # Poblar la relación `patient` explícitamente (evita el lazy-load async) para el detalle.
        consultation.patient = await db.get(Patient, consultation.patient_id)
        return ConsultationDetailResponse.model_validate(consultation)
    return ConsultationPatientResponse.model_validate(consultation)


@router.patch(
    "/{consultation_id}",
    response_model=ConsultationResponse,
    summary="Actualizar consulta (estado / asignación / notas)",
    responses={
        **_NOT_FOUND,
        409: {"description": "La consulta está asignada a otro médico."},
        422: {"description": "`status` inválido."},
    },
)
async def update_consultation(
    consultation_id: uuid.UUID,
    payload: ConsultationUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.write")),
) -> ConsultationResponse:
    return await consultations_service.update_consultation(
        db,
        consultation_id,
        payload,
        actor_user_id=principal.id,
        actor_is_admin=principal.is_admin,
    )


@router.delete(
    "/{consultation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar consulta (admin)",
    responses=_NOT_FOUND,
)
async def delete_consultation(
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.delete")),
) -> None:
    await consultations_service.delete_consultation(db, consultation_id, deleted_by=principal.id)


# --- Acciones de negocio (cierre, presencia, videoconsulta) ---


@router.post(
    "/{consultation_id}/close",
    response_model=ConsultationResponse,
    summary="Cerrar consulta o marcar ausencia (staff)",
    responses={**_NOT_FOUND, 409: {"description": "La consulta está asignada a otro médico."}},
)
async def close_consultation(
    consultation_id: uuid.UUID,
    payload: ConsultationCloseRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.close")),
) -> ConsultationResponse:
    """Cierra (`closed`) o marca `patient_no_show`, guarda la nota y registra el evento.
    El autor del cierre es el médico autenticado."""
    return await consultations_service.close_consultation(
        db,
        consultation_id,
        payload.outcome,
        closed_by=principal.id,
        note=payload.note,
        signature=payload.signature,
        actor_is_admin=principal.is_admin,
    )


@router.post(
    "/{consultation_id}/schedule-follow-up",
    response_model=ConsultationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agendar seguimiento: cierra esta consulta (firmada) y crea la hija agendada",
    responses={**_NOT_FOUND, 409: {"description": "La consulta está asignada a otro médico."}},
)
async def schedule_follow_up(
    consultation_id: uuid.UUID,
    payload: ScheduleFollowUpRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.close")),
) -> ConsultationResponse:
    """Cierra la consulta actual (firmada) y crea una consulta HIJA agendada para otra fecha,
    continuando la cadena de seguimiento. Devuelve la consulta hija creada."""
    child = await consultations_service.schedule_follow_up(
        db,
        parent_id=consultation_id,
        scheduled_at=payload.scheduled_at,
        closing_note=payload.closing_note,
        signature=payload.signature,
        actor_user_id=principal.id,
        actor_is_admin=principal.is_admin,
    )
    await _queue_appointment_email(background_tasks, db, child)
    return child


@router.post(
    "/{consultation_id}/refer",
    response_model=ConsultationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agendar con especialista: entrega esta consulta (derivada) y agenda con otro médico",
    responses={**_NOT_FOUND, 409: {"description": "La consulta está asignada a otro médico."}},
)
async def refer_to_specialist(
    consultation_id: uuid.UUID,
    payload: ScheduleReferralRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.close")),
) -> ConsultationResponse:
    """Deriva la consulta a OTRO médico: la actual queda 'referred_to_specialist' y se crea una
    hija agendada asignada al especialista, con el motivo firmado. Devuelve la consulta hija."""
    child = await consultations_service.schedule_referral(
        db,
        parent_id=consultation_id,
        invited_doctor_id=payload.invited_doctor_id,
        scheduled_at=payload.scheduled_at,
        reason=payload.reason,
        signature=payload.signature,
        actor_user_id=principal.id,
        actor_is_admin=principal.is_admin,
    )
    await _queue_appointment_email(background_tasks, db, child)
    # Email "te refirieron una cita" al especialista (si lo tiene habilitado; opt-out).
    ref_text = (
        "Un colega te refirió un paciente para una cita.\n\n"
        f"Fecha y hora: {notifications.fmt_when(payload.scheduled_at)}\n"
        f"Motivo: {payload.reason}\n"
        f"Código de caso: {child.code}\n\n"
        "Ingresa a tu agenda en Médicos por Venezuela.\n"
    )
    ref_args = await notifications.doctor_event_email_args(
        db,
        user_id=payload.invited_doctor_id,
        event="referral_received",
        subject="Te refirieron un paciente",
        text=ref_text,
    )
    if ref_args:
        background_tasks.add_task(notifications.send_mail, **ref_args)
    return child


@router.get(
    "/{consultation_id}/chain",
    response_model=list[ChainItem],
    summary="Historial de la cadena de seguimiento (padre→hijas) de una consulta",
    responses=_NOT_FOUND,
)
async def consultation_chain(
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("consultations.read")),
) -> list[ChainItem]:
    """Todas las consultas de la cadena (raíz + descendientes) a la que pertenece esta consulta,
    ordenadas — para ver el historial de seguimiento completo."""
    chain = await consultations_service.get_chain(db, consultation_id)
    return [ChainItem.model_validate(c) for c in chain]


@router.post(
    "/{consultation_id}/claim",
    response_model=ConsultationResponse,
    summary="Tomar una consulta en espera (claim atómico)",
    responses={
        **_NOT_FOUND,
        403: {"description": "El caso no corresponde a la especialidad del médico."},
        409: {"description": "La consulta ya fue tomada por otro médico."},
    },
)
async def claim_consultation(
    consultation_id: uuid.UUID,
    payload: ConsultationClaimRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.take")),
) -> ConsultationResponse:
    """El médico autenticado toma un caso en espera. Atómico: si otro médico lo tomó primero
    responde 409 (nunca dos médicos sobre el mismo paciente). `via_whatsapp` marca atención
    por WhatsApp (sin sala de video)."""
    consultation = await consultations_service.claim_consultation(
        db,
        consultation_id,
        doctor_user_id=principal.id,
        via_whatsapp=payload.via_whatsapp,
        doctor_specialty_id=principal.specialty_id,
        is_admin=principal.is_admin,
    )
    return ConsultationResponse.model_validate(consultation)


@router.post(
    "/{consultation_id}/entered-call",
    response_model=ConsultationResponse,
    summary="Marcar que el paciente entró a la videollamada (idempotente, sin sesión)",
    responses={**_NOT_FOUND, **_TOKEN_RESPONSES},
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def mark_entered_call(
    request: Request,
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_consultation_token),
) -> ConsultationResponse:
    """Registra `entered_call_at` una sola vez, si la consulta está en `waiting`/`in_progress`.
    Reemplaza la RPC mark_patient_entered_call. Sin sesión: el paciente en la sala puede no
    estar autenticado, pero debe presentar el token de acceso de SU consulta."""
    return await consultations_service.mark_entered_call(db, consultation_id)


@router.post(
    "/{consultation_id}/video-room",
    response_model=ConsultationResponse,
    summary="Generar/obtener la sala de video (idempotente, sin sesión)",
    responses={
        **_NOT_FOUND,
        **_TOKEN_RESPONSES,
        409: {"description": "La consulta no está en espera."},
    },
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def ensure_video_room(
    request: Request,
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_consultation_token),
) -> ConsultationResponse:
    """Genera la sala Jitsi si no existe (solo en estado `waiting`); si ya existe, devuelve la
    misma URL (idempotente). Exige el token de acceso de ESA consulta: devolver la URL de una
    videoconsulta médica a quien solo conozca el id era el hallazgo M3."""
    return await consultations_service.ensure_video_room(db, consultation_id)


# --- Eventos / auditoría de la consulta ---


@router.get(
    "/{consultation_id}/events",
    response_model=list[ConsultationEventResponse],
    summary="Listar eventos de la consulta (staff)",
    responses=_NOT_FOUND,
)
async def list_consultation_events(
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("consultations.read")),
) -> list[ConsultationEventResponse]:
    """Historial de auditoría de la consulta (cronológico)."""
    return await consultations_service.list_events(db, consultation_id)


@router.post(
    "/{consultation_id}/events",
    response_model=ConsultationEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar evento de la consulta (staff)",
    responses={
        **_NOT_FOUND,
        400: {"description": "El `consultation_id` del cuerpo no coincide con la ruta."},
        409: {"description": "La consulta está asignada a otro médico."},
    },
)
async def create_consultation_event(
    consultation_id: uuid.UUID,
    payload: ConsultationEventCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("consultations.write")),
) -> ConsultationEventResponse:
    return await consultations_service.create_event(
        db,
        consultation_id,
        payload,
        created_by=principal.id,
        actor_is_admin=principal.is_admin,
    )
