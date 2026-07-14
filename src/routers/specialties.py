"""HTTP layer for specialties."""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.specialty import (
    SpecialtyCatalogResponse,
    SpecialtyCreate,
    SpecialtyResponse,
    SpecialtyUpdate,
)
from src.services import specialties as specialties_service

router = APIRouter(prefix="/specialties", tags=["specialties"])
tag_metadata = [
    {
        "name": "specialties",
        "description": "Catálogo público y gestión admin/super_admin de especialidades.",
    }
]

_NOT_FOUND = {404: {"description": "Especialidad no encontrada."}}
_VALIDATION = {422: {"description": "Payload inválido."}}


@router.get(
    "",
    response_model=list[SpecialtyResponse],
    summary="Listar especialidades",
    responses=_VALIDATION,
)
async def list_specialties(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[SpecialtyResponse]:
    """Lista pública de especialidades activas; no requiere Bearer token."""
    return await specialties_service.list_specialties(db, skip=skip, limit=limit, status="active")


@router.get(
    "/catalog",
    response_model=SpecialtyCatalogResponse,
    summary="Catálogo de necesidades y reglas de matching",
)
async def get_catalog() -> SpecialtyCatalogResponse:
    """Devuelve las reglas estáticas de matching que usa la cola."""
    return SpecialtyCatalogResponse(
        specialties=specialties_service.SPECIALTIES,
        needs=specialties_service.NEEDS,
        specialty_needs=specialties_service.SPECIALTY_NEEDS,
        reserved_needs=specialties_service.RESERVED_NEEDS,
    )


@router.get(
    "/admin",
    response_model=list[SpecialtyResponse],
    summary="Listar especialidades, incluidas inactivas (admin)",
    responses={403: {"description": "Requiere el permiso catalogs.manage."}},
)
async def list_specialties_admin(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("catalogs.manage")),
) -> list[SpecialtyResponse]:
    """Lista completa para gestión (activas + inactivas). Requiere `catalogs.manage`."""
    return await specialties_service.list_specialties(db, skip=skip, limit=limit)


@router.post(
    "",
    response_model=SpecialtyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear especialidad (admin)",
    responses={**_VALIDATION, 409: {"description": "Conflicto de integridad."}},
)
async def create_specialty(
    payload: SpecialtyCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("catalogs.manage")),
) -> SpecialtyResponse:
    """Crea una especialidad. Requiere rol admin o super_admin."""
    return await specialties_service.create_specialty(db, payload, actor_user_id=principal.id)


@router.get(
    "/{specialty_id}",
    response_model=SpecialtyResponse,
    summary="Obtener especialidad (admin)",
    responses=_NOT_FOUND,
)
async def get_specialty(
    specialty_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("catalogs.manage")),
) -> SpecialtyResponse:
    """Devuelve una especialidad no eliminada. Requiere rol admin o super_admin."""
    return await specialties_service.get_specialty(db, specialty_id)


@router.patch(
    "/{specialty_id}",
    response_model=SpecialtyResponse,
    summary="Modificar especialidad (admin)",
    responses={**_NOT_FOUND, **_VALIDATION, 409: {"description": "Conflicto de integridad."}},
)
async def update_specialty(
    specialty_id: uuid.UUID,
    payload: SpecialtyUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("catalogs.manage")),
) -> SpecialtyResponse:
    """Modifica `name` y/o `status`. Requiere rol admin o super_admin."""
    return await specialties_service.update_specialty(
        db, specialty_id, payload, actor_user_id=principal.id
    )


@router.delete(
    "/{specialty_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft delete de especialidad (admin)",
    responses=_NOT_FOUND,
)
async def delete_specialty(
    specialty_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("catalogs.manage")),
) -> None:
    """Marca la especialidad como eliminada con `deleted_at`."""
    await specialties_service.delete_specialty(db, specialty_id, actor_user_id=principal.id)
