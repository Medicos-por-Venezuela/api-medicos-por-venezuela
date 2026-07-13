"""Gestión de roles de usuarios: catálogo, listar, asignar y revocar. Auditado.

Asignar/revocar registran una entrada en `audit_log` en la MISMA transacción que el
cambio (atómico): si el commit falla, no queda ni el cambio ni el audit.
"""

import uuid

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from src.core.errors import ConflictError, ForbiddenError, NotFoundError, UnprocessableError
from src.models.profile import Profile
from src.models.rbac import Role, UserRole
from src.services import audit


async def list_roles(session: AsyncSession, skip: int = 0, limit: int = 100) -> list[Role]:
    stmt = (
        select(Role).where(Role.deleted_at.is_(None)).order_by(Role.code).offset(skip).limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_user_roles(session: AsyncSession, user_id: uuid.UUID) -> list[Row]:
    """Filas (UserRole, Role) de los roles activos del usuario."""
    stmt = (
        select(UserRole, Role)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id == user_id,
            UserRole.revoked_at.is_(None),
            Role.deleted_at.is_(None),
        )
        .order_by(Role.code)
    )
    return list((await session.execute(stmt)).all())


async def _get_role(session: AsyncSession, code: str) -> Role:
    role = (
        await session.execute(select(Role).where(Role.code == code, Role.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if role is None:
        raise UnprocessableError(f"El rol '{code}' no existe.")
    return role


async def assign_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    role_code: str,
    actor_user_id: uuid.UUID,
    actor_roles: frozenset[str],
) -> tuple[UserRole, Role]:
    # Único punto de aplicación del guard: otorgar 'super_admin' exige que el actor YA
    # sea super_admin, sin importar si además tiene 'roles.assign'. Va antes de cualquier
    # lectura/escritura para no dejar fila ni audit de un intento denegado.
    if role_code == "super_admin" and "super_admin" not in actor_roles:
        raise ForbiddenError("Solo un super_admin puede otorgar el rol super_admin.")

    if await session.get(Profile, user_id) is None:
        raise NotFoundError("Usuario no encontrado.")
    role = await _get_role(session, role_code)

    existing = (
        await session.execute(
            select(UserRole).where(
                UserRole.user_id == user_id,
                UserRole.role_id == role.id,
                UserRole.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("El usuario ya tiene ese rol asignado.")

    user_role = UserRole(user_id=user_id, role_id=role.id, assigned_by=actor_user_id)
    session.add(user_role)
    await audit.log_action(
        session,
        action="role.assigned",
        actor_user_id=actor_user_id,
        resource="user_roles",
        resource_id=user_id,
        metadata={"role": role.code, "target_user_id": str(user_id)},
    )
    await session.commit()
    await session.refresh(user_role)
    return user_role, role


async def revoke_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_roles: frozenset[str],
) -> None:
    row = (
        await session.execute(
            select(UserRole, Role)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                UserRole.user_id == user_id,
                UserRole.role_id == role_id,
                UserRole.revoked_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("El usuario no tiene ese rol activo.")
    user_role, role = row

    # Mismo guard que assign_role: revocar 'super_admin' exige que el actor YA
    # sea super_admin, para que ningún admin plano pueda des-escalar (o
    # auto-blindarse) a un super_admin solo con 'roles.assign'.
    if role.code == "super_admin" and "super_admin" not in actor_roles:
        raise ForbiddenError("Solo un super_admin puede revocar el rol super_admin.")

    user_role.revoked_at = func.now()
    await audit.log_action(
        session,
        action="role.revoked",
        actor_user_id=actor_user_id,
        resource="user_roles",
        resource_id=user_id,
        metadata={"role_id": str(role_id), "target_user_id": str(user_id)},
    )
    await session.commit()
