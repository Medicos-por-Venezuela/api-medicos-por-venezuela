"""Capa HTTP (delgada) para profiles. La lógica vive en src.services.profiles.

Autorización: listar es admin; leer un perfil es staff; la presencia y la
finalización de rol operan sobre el propio usuario (del JWT); revocar es admin.
"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import (
    Principal,
    get_current_principal,
    require_permission,
    require_staff,
)
from src.db.session import get_db
from src.schemas.profile import (
    ProfileActiveRequest,
    ProfileFinalizeRoleRequest,
    ProfileListResponse,
    ProfileResponse,
)
from src.services import profiles as profiles_service

router = APIRouter(prefix="/profiles", tags=["profiles"])
tag_metadata = [
    {
        "name": "profiles",
        "description": "Perfiles de cuentas (staff): lectura, presencia, revocación y rol.",
    }
]

_NOT_FOUND = {404: {"description": "Perfil no encontrado."}}


@router.get("", response_model=ProfileListResponse, summary="Listar perfiles (admin)")
async def list_profiles(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    role: str | None = Query(None, description="Un rol exacto (retrocompatible)."),
    roles: list[str] | None = Query(None, description="Uno o varios roles (p. ej. staff)."),
    search: str | None = Query(None, description="Filtra por nombre, email o especialidad."),
    active: bool | None = Query(None, description="true=activos, false=revocados, omitir=ambos."),
    created_from: date | None = Query(None),
    created_to: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("profiles.read")),
) -> ProfileListResponse:
    """Lista paginada de perfiles + total exacto. Filtros: `role`/`roles`, `search` (nombre/email/
    especialidad), `active` (revocado o no) y rango de fechas. Reemplaza el acceso directo del
    panel admin a la tabla `users`."""
    items, total = await profiles_service.list_profiles(
        db,
        skip=skip,
        limit=limit,
        role=role,
        roles=roles,
        search=search,
        active=active,
        created_from=created_from,
        created_to=created_to,
    )
    return ProfileListResponse(
        items=[ProfileResponse.model_validate(p) for p in items], total=total
    )


@router.post(
    "/me/finalize-role",
    response_model=ProfileResponse,
    summary="Finalizar el rol del usuario autenticado (patient/doctor)",
    responses={400: {"description": "Rol inválido o ya elegido."}},
)
async def finalize_role(
    payload: ProfileFinalizeRoleRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ProfileResponse:
    """Réplica de `set_my_role`: finaliza el propio perfil una vez como `patient`/`doctor`."""
    return await profiles_service.finalize_role(
        db,
        principal.id,
        role=payload.role,
        specialty_id=payload.specialty_id,
        country=payload.country,
        medical_license=payload.medical_license,
        whatsapp_number=payload.whatsapp_number,
    )


@router.get(
    "/{profile_id}",
    response_model=ProfileResponse,
    summary="Obtener perfil (staff)",
    responses=_NOT_FOUND,
)
async def get_profile(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_staff),
) -> ProfileResponse:
    return await profiles_service.get_profile(db, profile_id)


@router.patch(
    "/{profile_id}/active",
    response_model=ProfileResponse,
    summary="Revocar / reactivar médico (admin)",
    responses=_NOT_FOUND,
)
async def set_active(
    profile_id: uuid.UUID,
    payload: ProfileActiveRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("profiles.manage")),
) -> ProfileResponse:
    """Revoca (`active=false`) o reactiva (`active=true`) el acceso de un médico."""
    return await profiles_service.set_active(
        db, profile_id, payload.active, actor_user_id=principal.id
    )
