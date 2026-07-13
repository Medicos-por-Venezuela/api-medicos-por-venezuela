"""Capa HTTP (delgada) para la gestión de roles de usuarios (RBAC).

Autorización: todo requiere el permiso `roles.assign`. Asignar/revocar quedan
auditados (actor = quien ejecuta, del JWT).
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.user_role import RoleAssignRequest, RoleResponse, UserRoleResponse
from src.services import user_roles as user_roles_service

router = APIRouter(tags=["rbac"])
tag_metadata = [
    {"name": "rbac", "description": "Roles y permisos: catálogo y asignación a usuarios."}
]

# Todo el manejo de roles exige el permiso `roles.assign`.
_require_manage = require_permission("roles.assign")

_NOT_FOUND = {404: {"description": "Usuario o rol no encontrado."}}


@router.get("/roles", response_model=list[RoleResponse], summary="Catálogo de roles")
async def list_roles(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(_require_manage),
) -> list[RoleResponse]:
    return await user_roles_service.list_roles(db, skip=skip, limit=limit)


@router.get(
    "/users/{user_id}/roles",
    response_model=list[UserRoleResponse],
    summary="Roles activos de un usuario",
)
async def list_user_roles(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(_require_manage),
) -> list[UserRoleResponse]:
    rows = await user_roles_service.list_user_roles(db, user_id)
    return [
        UserRoleResponse(
            id=ur.id,
            role_id=role.id,
            role_code=role.code,
            role_name=role.name,
            assigned_at=ur.assigned_at,
        )
        for ur, role in rows
    ]


@router.post(
    "/users/{user_id}/roles",
    response_model=UserRoleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Asignar un rol a un usuario",
    responses={
        **_NOT_FOUND,
        403: {"description": "Solo un super_admin puede otorgar el rol super_admin."},
        409: {"description": "El usuario ya tiene ese rol."},
    },
)
async def assign_role(
    user_id: uuid.UUID,
    payload: RoleAssignRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(_require_manage),
) -> UserRoleResponse:
    """Asigna `role_code` al usuario (multi-rol: puede tener varios). Auditado.

    Otorgar `super_admin` exige que el propio actor ya sea `super_admin`
    (ver `assign_role` en `services/user_roles.py`), incluso si el actor tiene
    `roles.assign` por otro rol (p. ej. un `admin` plano).
    """
    user_role, role = await user_roles_service.assign_role(
        db, user_id, payload.role_code, actor_user_id=principal.id, actor_roles=principal.roles
    )
    return UserRoleResponse(
        id=user_role.id,
        role_id=role.id,
        role_code=role.code,
        role_name=role.name,
        assigned_at=user_role.assigned_at,
    )


@router.delete(
    "/users/{user_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revocar un rol de un usuario",
    responses={
        **_NOT_FOUND,
        403: {"description": "Solo un super_admin puede revocar el rol super_admin."},
    },
)
async def revoke_role(
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(_require_manage),
) -> None:
    """Revoca (soft) el rol; conserva el historial en user_roles. Auditado.

    Revocar `super_admin` exige que el propio actor ya sea `super_admin`
    (mismo guard simétrico que `assign_role`), incluso si el actor tiene
    `roles.assign` por otro rol (p. ej. un `admin` plano).
    """
    await user_roles_service.revoke_role(db, user_id, role_id, principal.id, principal.roles)
