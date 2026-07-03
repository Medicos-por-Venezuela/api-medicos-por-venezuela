"""HTTP layer for affected_zones."""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_admin
from src.db.session import get_db
from src.schemas.affected_zone import (
    AffectedZoneCreate,
    AffectedZonePublicResponse,
    AffectedZoneResponse,
    AffectedZoneUpdate,
)
from src.services import affected_zones as affected_zones_service

router = APIRouter(prefix="/affected-zones", tags=["affected-zones"])
tag_metadata = [
    {
        "name": "affected-zones",
        "description": (
            "Zonas afectadas por el terremoto. "
            "Listado público; gestión restringida a admin/super_admin."
        ),
    }
]

_NOT_FOUND = {404: {"description": "Zona afectada no encontrada."}}
_VALIDATION = {422: {"description": "Payload inválido."}}


@router.get(
    "/list",
    response_model=list[AffectedZonePublicResponse],
    summary="Listar zonas afectadas activas",
    responses=_VALIDATION,
)
async def list_affected_zones(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[AffectedZonePublicResponse]:
    """Lista pública de zonas afectadas activas; no requiere Bearer token."""
    return await affected_zones_service.list_affected_zones(
        db, skip=skip, limit=limit, status="active"
    )


@router.get(
    "/admin",
    response_model=list[AffectedZoneResponse],
    summary="Listar zonas afectadas (admin)",
    responses=_VALIDATION,
)
async def list_affected_zones_admin(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> list[AffectedZoneResponse]:
    """Lista completa para admin: activas + inactivas (no eliminadas)."""
    return await affected_zones_service.list_affected_zones(db, skip=skip, limit=limit)


@router.get(
    "/{zone_id}",
    response_model=AffectedZonePublicResponse,
    summary="Obtener zona afectada activa",
    responses=_NOT_FOUND,
)
async def get_affected_zone(
    zone_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> AffectedZonePublicResponse:
    """Devuelve una zona afectada activa por ID; no requiere Bearer token."""
    return await affected_zones_service.get_active_affected_zone(db, zone_id)


@router.post(
    "",
    response_model=AffectedZoneResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear zona afectada (admin)",
    responses={**_VALIDATION, 409: {"description": "Conflicto de integridad."}},
)
async def create_affected_zone(
    payload: AffectedZoneCreate,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> AffectedZoneResponse:
    """Crea una zona afectada. Requiere rol admin o super_admin."""
    return await affected_zones_service.create_affected_zone(db, payload)


@router.patch(
    "/{zone_id}",
    response_model=AffectedZoneResponse,
    summary="Modificar zona afectada (admin)",
    responses={**_NOT_FOUND, **_VALIDATION, 409: {"description": "Conflicto de integridad."}},
)
async def update_affected_zone(
    zone_id: uuid.UUID,
    payload: AffectedZoneUpdate,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> AffectedZoneResponse:
    """Modifica nombre, estado, país y/o status. Requiere rol admin o super_admin."""
    return await affected_zones_service.update_affected_zone(db, zone_id, payload)


@router.delete(
    "/{zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft delete de zona afectada (admin)",
    responses=_NOT_FOUND,
)
async def delete_affected_zone(
    zone_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> None:
    """Marca la zona afectada como eliminada con deleted_at."""
    await affected_zones_service.delete_affected_zone(db, zone_id)
