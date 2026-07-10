"""Endpoints de la sesión autenticada (la identidad sale del JWT de Supabase)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, get_current_principal
from src.db.session import get_db
from src.schemas.auth import PrincipalPermissionsResponse
from src.schemas.profile import ProfileResponse
from src.services import profiles as profiles_service

router = APIRouter(prefix="/auth", tags=["auth"])
tag_metadata = [
    {"name": "auth", "description": "Sesión autenticada: la identidad sale del JWT de Supabase."}
]


@router.get(
    "/me",
    response_model=ProfileResponse,
    summary="Perfil del usuario autenticado",
    responses={401: {"description": "No autenticado / token inválido."}},
)
async def me(
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ProfileResponse:
    """Devuelve el perfil del titular del JWT (reemplaza getSession + cargar profile)."""
    return await profiles_service.get_profile(db, principal.id)


@router.get(
    "/me/permissions",
    response_model=PrincipalPermissionsResponse,
    summary="Roles y permisos RBAC efectivos del usuario autenticado",
    responses={401: {"description": "No autenticado / token inválido."}},
)
async def me_permissions(
    principal: Principal = Depends(get_current_principal),
) -> PrincipalPermissionsResponse:
    """Roles y permisos efectivos (unión de todos sus roles activos) del titular del
    JWT — para que el cliente condicione la UI sin adivinar a partir del `role`
    legado de `GET /auth/me` (que es un único valor, no el set RBAC real)."""
    return PrincipalPermissionsResponse(
        roles=sorted(principal.roles), permissions=sorted(principal.permissions)
    )
