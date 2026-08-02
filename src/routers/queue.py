"""Capa HTTP (delgada) de la cola de pacientes (Board).

Punto crítico de producción: dos médicos pueden tomar el mismo paciente en el
mismo milisegundo. El servicio usa with_for_update(nowait=True); aquí se traduce
el error de lock (fila bloqueada) en un 409 con mensaje específico de dominio.

El médico que toma el caso es SIEMPRE el titular del JWT (no se confía en ids del
cliente): evita IDOR.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import is_lock_not_available
from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.consultation import ConsultationResponse, QueueReleaseResponse
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
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("queue.read")),
) -> list[ConsultationResponse]:
    """Lista las consultas en estado `waiting`, las más antiguas primero (FIFO)."""
    return await queue_service.list_queue(db, limit=limit)


@router.post(
    "/{consultation_id}/take",
    response_model=ConsultationResponse,
    summary="Tomar una consulta de la cola (atómico)",
    responses={
        200: {"description": "Consulta asignada al médico (pasa a `in_progress`)."},
        404: {"description": "La consulta no existe o ya no está en espera."},
        409: {"description": "Otro médico la está tomando en este instante (fila bloqueada)."},
    },
)
async def take_consultation(
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("queue.take")),
) -> ConsultationResponse:
    """Asignación **atómica anti-colisión** de una consulta en espera al médico
    autenticado. El ganador recibe `200`, el perdedor `409` (o `404`), sin colgarse.
    """
    try:
        return await queue_service.take_consultation(db, consultation_id, principal.id)
    except DBAPIError as exc:
        if not is_lock_not_available(exc):
            raise
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_LOCK_DETAIL) from None


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
