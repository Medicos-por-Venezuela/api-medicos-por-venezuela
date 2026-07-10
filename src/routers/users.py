"""Capa HTTP (delgada) para la creación administrativa de usuarios de Auth.

Autorización: `users.create` (seeded para `admin`/`super_admin`, ver
`db/migrations/20260709_000001_seed_users_create_permission.sql`).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.user import UserCreate, UserResponse
from src.services import users as users_service

router = APIRouter(prefix="/users", tags=["users"])
tag_metadata = [
    {
        "name": "users",
        "description": "Creación administrativa de usuarios de Auth (Supabase Admin API).",
    }
]

_require_create = require_permission("users.create")


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un usuario de Auth (+ rol inicial opcional)",
    responses={
        403: {"description": "El principal no tiene el permiso 'users.create'."},
        409: {"description": "Ya existe un usuario de Auth con ese correo."},
        422: {
            "description": (
                "Rol inicial inválido, o 'super_admin' como rol inicial "
                "(no permitido; usa POST /users/{id}/roles)."
            )
        },
        502: {"description": "El proveedor de autenticación (Supabase Admin API) falló."},
    },
)
async def create_user(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(_require_create),
) -> UserResponse:
    """Crea un usuario de Supabase Auth (email + password) vía la Admin API.

    El trigger `handle_new_auth_user()` crea la fila correspondiente en
    `public.users`. Si se envía `initial_role` (`patient`/`doctor`/`admin`), se
    asigna reutilizando el servicio existente de roles (auditado, `role.assigned`).
    `super_admin` NUNCA se puede otorgar por esta vía, ni siquiera para un actor
    que ya sea `super_admin`: usa `POST /users/{id}/roles`.
    """
    profile, effective_role = await users_service.create_user(
        db, payload, principal.id, principal.roles
    )
    return UserResponse(
        id=profile.id,
        email=profile.email,
        full_name=profile.full_name,
        role=effective_role,
        active=profile.active,
        verified=profile.verified,
        created_at=profile.created_at,
    )
