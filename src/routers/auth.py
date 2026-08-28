"""Endpoints de la sesión autenticada (la identidad sale del JWT de Supabase)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError
from src.core.security import Principal, effective_role, get_current_principal
from src.db.session import get_db
from src.schemas.auth import PrincipalPermissionsResponse
from src.schemas.profile import MyProfileResponse
from src.services import doctors as doctors_service
from src.services import profiles as profiles_service

router = APIRouter(prefix="/auth", tags=["auth"])
tag_metadata = [
    {"name": "auth", "description": "Sesión autenticada: la identidad sale del JWT de Supabase."}
]


@router.get(
    "/me",
    response_model=MyProfileResponse,
    summary="Perfil del usuario autenticado",
    responses={401: {"description": "No autenticado / token inválido."}},
)
async def me(
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> MyProfileResponse:
    """Perfil del titular del JWT (reemplaza getSession + cargar profile). Incluye el contexto de
    médico (`has_doctor_profile`/`doctor_cedula`), con la MISMA lógica que `/doctors/me`, para que
    el panel decida el redirect sin una segunda llamada.

    `role` = el rol EFECTIVO más alto del RBAC (`user_roles`; un dual doctor+super_admin se
    presenta como super_admin) y `roles` = la lista completa. La columna `users.role` es un
    único valor legado — el Principal ya trae los roles reales (con fallback al legado para
    cuentas sin filas RBAC), así que esto no agrega queries."""
    profile = await profiles_service.get_profile(db, principal.id)
    resp = MyProfileResponse.model_validate(profile)
    resp.roles = sorted(principal.roles)
    resp.role = effective_role(principal.roles) or resp.role
    try:
        doctor_me = await doctors_service.get_my_profile(db, principal.id)
        resp.has_doctor_profile = True
        resp.doctor_cedula = doctor_me.cedula
    except NotFoundError:
        # Admin puro (sin perfil de médico): no es médico, no se redirige a completar cédula.
        resp.has_doctor_profile = False
    return resp


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
    legado de `GET /auth/me` (que es un único valor, no el set RBAC real).

    Un médico cuya credencial aún no está verificada llega con `permissions` vacío y
    `credential_verified: false`: tiene el rol pero no puede atender hasta que el SACS/FPV
    valide su cédula o un admin apruebe su ficha."""
    return PrincipalPermissionsResponse(
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
        credential_verified=principal.credential_verified,
    )
