"""HTTP layer for professional_types."""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.professional_type import (
    ProfessionalTypeCreate,
    ProfessionalTypeResponse,
    ProfessionalTypeUpdate,
)
from src.services import professional_types as professional_types_service

router = APIRouter(prefix="/professional-types", tags=["professional-types"])
tag_metadata = [
    {
        "name": "professional-types",
        "description": "Admin-managed professional type catalog.",
    }
]

_NOT_FOUND = {404: {"description": "Professional type not found."}}


@router.get(
    "",
    response_model=list[ProfessionalTypeResponse],
    summary="List professional types",
)
async def list_professional_types(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[ProfessionalTypeResponse]:
    """List non-deleted professional types; no Bearer token required."""
    return await professional_types_service.list_professional_types(db, skip=skip, limit=limit)


@router.post(
    "",
    response_model=ProfessionalTypeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create professional type (admin)",
    responses={422: {"description": "Invalid payload."}},
)
async def create_professional_type(
    payload: ProfessionalTypeCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("catalogs.manage")),
) -> ProfessionalTypeResponse:
    """Create a professional type with `status=active` by default."""
    return await professional_types_service.create_professional_type(
        db, payload, actor_user_id=principal.id
    )


@router.get(
    "/{professional_type_id}",
    response_model=ProfessionalTypeResponse,
    summary="Get professional type (staff)",
    responses=_NOT_FOUND,
)
async def get_professional_type(
    professional_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("catalogs.manage")),
) -> ProfessionalTypeResponse:
    """Get a non-deleted professional type by ID."""
    return await professional_types_service.get_professional_type(db, professional_type_id)


@router.patch(
    "/{professional_type_id}",
    response_model=ProfessionalTypeResponse,
    summary="Update professional type (admin)",
    responses={**_NOT_FOUND, 422: {"description": "Invalid payload."}},
)
async def update_professional_type(
    professional_type_id: uuid.UUID,
    payload: ProfessionalTypeUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("catalogs.manage")),
) -> ProfessionalTypeResponse:
    """Update name/status for a non-deleted professional type."""
    return await professional_types_service.update_professional_type(
        db, professional_type_id, payload, actor_user_id=principal.id
    )


@router.delete(
    "/{professional_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete professional type (admin)",
    responses=_NOT_FOUND,
)
async def delete_professional_type(
    professional_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("catalogs.manage")),
) -> None:
    """Mark the professional type as `deleted`; the row remains in the table."""
    await professional_types_service.delete_professional_type(
        db, professional_type_id, actor_user_id=principal.id
    )
