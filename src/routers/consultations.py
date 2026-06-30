"""Capa HTTP (delgada) para consultations y sus eventos.

Autorización (replica las RLS):
- Crear consulta / heartbeat / sala de video: público (auto-servicio del paciente anónimo).
- Leer: staff ve todo; un paciente autenticado solo ve lo suyo (anti-IDOR).
- Actualizar / cerrar / eventos: staff. Eliminar: admin.
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import (
    Principal,
    get_current_principal,
    require_admin,
    require_staff,
)
from src.db.session import get_db
from src.schemas.consultation import (
    ConsultationCloseRequest,
    ConsultationCreate,
    ConsultationPatientResponse,
    ConsultationResponse,
    ConsultationUpdate,
)
from src.schemas.consultation_event import (
    ConsultationEventCreate,
    ConsultationEventResponse,
)
from src.services import consultations as consultations_service

router = APIRouter(prefix="/consultations", tags=["consultations"])
tag_metadata = [
    {
        "name": "consultations",
        "description": "Casos/consultas y su historial de eventos (auditoría).",
    }
]

_NOT_FOUND = {404: {"description": "Consulta no encontrada."}}


@router.get(
    "",
    response_model=list[ConsultationResponse] | list[ConsultationPatientResponse],
    summary="Listar consultas",
)
async def list_consultations(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    patient_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[ConsultationResponse] | list[ConsultationPatientResponse]:
    """Staff ve todas las consultas con vista completa.
    Un paciente autenticado solo ve las suyas, sin notas clínicas ni internas."""
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
        return [ConsultationResponse.model_validate(c) for c in consultations]
    return [ConsultationPatientResponse.model_validate(c) for c in consultations]


@router.post(
    "",
    response_model=ConsultationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear consulta (público)",
    responses={
        400: {"description": "El `patient_id` no existe."},
        422: {"description": "`status` inválido."},
    },
)
async def create_consultation(
    payload: ConsultationCreate, db: AsyncSession = Depends(get_db)
) -> ConsultationResponse:
    """Crea una consulta en espera. El `code` lo genera la base de datos (trigger)."""
    return await consultations_service.create_consultation(db, payload)


@router.get(
    "/{consultation_id}",
    response_model=ConsultationResponse | ConsultationPatientResponse,
    summary="Obtener consulta",
    responses=_NOT_FOUND,
)
async def get_consultation(
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ConsultationResponse | ConsultationPatientResponse:
    """Staff recibe la vista completa (incluye notas clínicas/internas).
    Un paciente autenticado solo recibe su propia consulta sin las notas del médico."""
    consultation = await consultations_service.get_consultation(
        db, consultation_id, viewer_is_staff=principal.is_staff, viewer_user_id=principal.id
    )
    if principal.is_staff:
        return ConsultationResponse.model_validate(consultation)
    return ConsultationPatientResponse.model_validate(consultation)


@router.patch(
    "/{consultation_id}",
    response_model=ConsultationResponse,
    summary="Actualizar consulta (estado / asignación / notas)",
    responses={**_NOT_FOUND, 422: {"description": "`status` inválido."}},
)
async def update_consultation(
    consultation_id: uuid.UUID,
    payload: ConsultationUpdate,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_staff),
) -> ConsultationResponse:
    return await consultations_service.update_consultation(db, consultation_id, payload)


@router.delete(
    "/{consultation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar consulta (admin)",
    responses=_NOT_FOUND,
)
async def delete_consultation(
    consultation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> None:
    await consultations_service.delete_consultation(db, consultation_id)


# --- Acciones de negocio (cierre, presencia, videoconsulta) ---


@router.post(
    "/{consultation_id}/close",
    response_model=ConsultationResponse,
    summary="Cerrar consulta o marcar ausencia (staff)",
    responses=_NOT_FOUND,
)
async def close_consultation(
    consultation_id: uuid.UUID,
    payload: ConsultationCloseRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_staff),
) -> ConsultationResponse:
    """Cierra (`closed`) o marca `patient_no_show`, guarda la nota y registra el evento.
    El autor del cierre es el médico autenticado."""
    return await consultations_service.close_consultation(
        db, consultation_id, payload.outcome, closed_by=principal.id, note=payload.note
    )


@router.post(
    "/{consultation_id}/heartbeat",
    response_model=ConsultationResponse,
    summary="Heartbeat de presencia del paciente (público)",
    responses=_NOT_FOUND,
)
async def patient_heartbeat(
    consultation_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ConsultationResponse:
    """Marca que el paciente sigue en la sala de espera (`patient_last_seen_at`).
    Solo tiene efecto si la consulta está en `waiting` o `in_progress`."""
    return await consultations_service.heartbeat(db, consultation_id)


@router.post(
    "/{consultation_id}/video-room",
    response_model=ConsultationResponse,
    summary="Generar/obtener la sala de video (idempotente, público)",
    responses={**_NOT_FOUND, 409: {"description": "La consulta no está en espera."}},
)
async def ensure_video_room(
    consultation_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ConsultationResponse:
    """Genera la sala Jitsi si no existe (solo en estado `waiting`); si ya existe,
    devuelve la misma URL (idempotente)."""
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
    _: Principal = Depends(require_staff),
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
    },
)
async def create_consultation_event(
    consultation_id: uuid.UUID,
    payload: ConsultationEventCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_staff),
) -> ConsultationEventResponse:
    return await consultations_service.create_event(
        db, consultation_id, payload, created_by=principal.id
    )
