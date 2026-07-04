"""Autorización: carga los roles y permisos efectivos de un usuario.

Fuente autoritativa: `user_roles` (roles activos) → `role_permissions` → `permissions`.
Coexistencia durante la migración: si el usuario aún no tiene roles asignados, cae al
rol de `profiles.role` (mapeando el legado 'specialist' → 'doctor').
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.models.rbac import Permission, Role, RolePermission, UserRole

# profiles.role legado -> rol RBAC.
_LEGACY_ROLE_MAP = {"specialist": "doctor"}


async def _roles_and_perms(
    session: AsyncSession, role_filter: ColumnElement[bool]
) -> tuple[set[str], set[str]]:
    """(codes de rol, codes de permiso) de los roles no borrados que cumplen el filtro."""
    stmt = (
        select(Role.code, Permission.code)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .outerjoin(Permission, Permission.id == RolePermission.permission_id)
        .where(Role.deleted_at.is_(None), role_filter)
    )
    rows = (await session.execute(stmt)).all()
    roles = {role_code for role_code, _ in rows}
    perms = {perm_code for _, perm_code in rows if perm_code is not None}
    return roles, perms


async def load_authz(
    session: AsyncSession, user_id: uuid.UUID, fallback_role: str | None
) -> tuple[frozenset[str], frozenset[str]]:
    """Roles y permisos efectivos del usuario.

    Toma los roles activos de `user_roles`; si no tiene ninguno todavía, cae al rol
    de `profiles.role` (coexistencia mientras se completa la migración).
    """
    active_role_ids = (
        select(UserRole.role_id)
        .where(UserRole.user_id == user_id, UserRole.revoked_at.is_(None))
        .scalar_subquery()
    )
    roles, perms = await _roles_and_perms(session, Role.id.in_(active_role_ids))

    if not roles and fallback_role:
        code = _LEGACY_ROLE_MAP.get(fallback_role, fallback_role)
        roles, perms = await _roles_and_perms(session, Role.code == code)

    return frozenset(roles), frozenset(perms)
