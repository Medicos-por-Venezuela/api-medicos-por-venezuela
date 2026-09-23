"""Capa HTTP (delgada) de la cola de pacientes (Board).

Punto crítico de producción: dos médicos pueden tomar el mismo paciente en el
mismo milisegundo. El servicio usa with_for_update(nowait=True); aquí se traduce
el error de lock (fila bloqueada) en un 409 con mensaje específico de dominio.

El médico que toma el caso es SIEMPRE el titular del JWT (no se confía en ids del
cliente): evita IDOR.

Contenido clínico: en la cola, SUMMARY (motivo) para el médico cuya cola incluye el caso; al
tomarlo, el tratante lo recibe completo. El admin que no ejerce, en null. Cada lectura concedida
queda en `audit_log` (`READ_CLINICAL_DATA`).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import is_lock_not_available
from src.core.observability import client_ip
from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.clinical import clinical_context
from src.schemas.consultation import ConsultationResponse, QueueReleaseResponse
from src.services import clinical_access, queue_access
from src.services import queue as queue_service

router = APIRouter(prefix="/queue", tags=["queue"])
tag_metadata = [
    {
        "name": "queue",
        "description": (
            "Cola de pacientes (Board) en tiempo real. Incluye la **toma atómica** "
            "anti-colisión de una consulta por un médico."
        ),
    }
]

_LOCK_DETAIL = (
    "Este paciente está siendo seleccionado por otro médico en este momento. Inténtalo de nuevo."
)


@router.get(
    "",
    response_model=list[ConsultationResponse],
    summary="Board: consultas en espera",
)
async def list_queue(
    request: Request,
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.read")),
) -> list[ConsultationResponse]:
    """Lista las consultas en espera sin asignar de las colas del médico (su especialidad y las
    que tenga habilitadas; un admin ve todas), las más antiguas primero (FIFO).

    Solo el motivo (`clinical_access = "summary"`) y solo en los casos de SUS colas como médico;
    las notas nunca. Un admin que no ejerce recibe lo clínico en null (`none`)."""
    scope = await queue_access.queue_scope(
        db,
        user_id=principal.id,
        specialty_id=principal.specialty_id,
        is_admin=principal.is_admin,
    )
    items = await queue_service.list_queue(db, scope, limit=limit)
    # El SUMMARY sale del alcance COMO MÉDICO (sin la vista global de admin): para un no-admin
    # es el mismo `scope`; al admin se le calcula aparte.
    clinical_scope = (
        scope
        if not principal.is_admin and clinical_access.practices_medicine(principal)
        else await clinical_access.queue_grant(db, principal)
    )
    grants = [
        clinical_access.grant_for_queue_item(
            principal,
            clinical_scope,
            assigned_doctor_id=c.assigned_doctor_id,
            specialty_id=c.specialty_id,
            status=c.status,
        )
        for c in items
    ]
    rows = [
        ConsultationResponse.model_validate(c, context=clinical_context(g))
        for c, g in zip(items, grants, strict=True)
    ]
    await clinical_access.audit_clinical_read(
        db,
        principal=principal,
        ip=client_ip(request),
        resource="consultations",
        grants=[(c.id, g) for c, g in zip(items, grants, strict=True)],
    )
    return rows


@router.post(
    "/{consultation_id}/take",
    response_model=ConsultationResponse,
    summary="Tomar una consulta de la cola (atómico)",
    responses={
        200: {"description": "Consulta asignada al médico (pasa a `in_progress`, con sala)."},
        403: {"description": "El caso no es de las colas del médico."},
        404: {"description": "La consulta no existe o ya no está en espera."},
        409: {"description": "Otro médico la está tomando en este instante (fila bloqueada)."},
    },
)
async def take_consultation(
    consultation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.take")),
) -> ConsultationResponse:
    """Asignación **atómica anti-colisión** de una consulta en espera al médico
    autenticado. El ganador recibe `200`, el perdedor `409` (o `404`), sin colgarse.
    Quien la toma queda como médico tratante: la respuesta trae lo clínico en claro (`full`) si
    ejerce; un admin que no ejerce lo recibe en null.
    """
    scope = await queue_access.queue_scope(
        db,
        user_id=principal.id,
        specialty_id=principal.specialty_id,
        is_admin=principal.is_admin,
    )
    try:
        consultation = await queue_service.take_consultation(
            db, consultation_id, principal.id, scope
        )
    except DBAPIError as exc:
        if not is_lock_not_available(exc):
            raise
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_LOCK_DETAIL) from None
    grant = clinical_access.treating_doctor_grant(principal, consultation.assigned_doctor_id)
    out = ConsultationResponse.model_validate(consultation, context=clinical_context(grant))
    await clinical_access.audit_clinical_read(
        db,
        principal=principal,
        ip=client_ip(request),
        resource="consultations",
        grants=[(consultation.id, grant)],
    )
    return out


@router.post(
    "/release-stale",
    response_model=QueueReleaseResponse,
    summary="Liberar consultas estancadas (resiliencia, admin)",
)
async def release_stale(
    minutes: int = Query(None, ge=1, description="Umbral en minutos (def. configurado)"),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("queue.manage")),
) -> QueueReleaseResponse:
    """Devuelve a la cola (`waiting`) las consultas `in_progress` abiertas hace más del
    umbral, liberándolas para otro médico. Pensado para invocarse desde un CRON/worker."""
    threshold = minutes or settings.STALE_CONSULTATION_MINUTES
    released = await queue_service.release_stale(db, threshold)
    return QueueReleaseResponse(released=released, threshold_minutes=threshold)
