"""Autenticación (valida el JWT de Supabase) y autorización (RBAC).

El login sigue en Supabase Auth: el frontend obtiene un JWT y lo envía como
`Authorization: Bearer <token>`. Aquí validamos firma/exp/audiencia, extraemos el
`sub` (= id del perfil) y cargamos el rol desde `profiles`. Las decisiones de rol
replican las políticas RLS (is_staff / is_admin).
"""

import logging
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.db.session import get_db
from src.models.profile import Profile

logger = logging.getLogger("mpv.api")

# Mapeo de roles (valores reales en la BD): medico = doctor, paciente = patient.
STAFF_ROLES = {"doctor", "specialist", "admin", "super_admin"}
ADMIN_ROLES = {"admin", "super_admin"}

# auto_error=False: gestionamos nosotros el 401 (mensaje uniforme).
_bearer = HTTPBearer(auto_error=False)


class Principal(BaseModel):
    """Identidad autenticada derivada del JWT + el perfil en BD."""

    id: uuid.UUID
    email: str | None = None
    role: str
    active: bool
    verified: bool
    specialty: str | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES and self.active and self.verified

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES and self.active


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def decode_token(token: str) -> dict:
    """Valida y decodifica el JWT de Supabase. Lanza 401 si es inválido/expirado."""
    try:
        return jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=[settings.SUPABASE_JWT_ALGORITHM],
            audience=settings.SUPABASE_JWT_AUDIENCE,
        )
    except jwt.PyJWTError as exc:
        logger.warning("SEC:token_invalid reason=%s", type(exc).__name__)
        raise _unauthorized("Token inválido o expirado.") from exc


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """Dependencia base: exige un JWT válido y un perfil existente."""
    if credentials is None:
        raise _unauthorized("No autenticado.")
    payload = decode_token(credentials.credentials)
    sub = payload.get("sub")
    if not sub:
        raise _unauthorized("Token sin 'sub'.")
    try:
        user_id = uuid.UUID(str(sub))
    except ValueError as exc:
        raise _unauthorized("Identificador de usuario inválido.") from exc

    profile = await db.get(Profile, user_id)
    if profile is None:
        logger.warning("SEC:profile_missing user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No existe un perfil para este usuario.",
        )
    return Principal(
        id=profile.id,
        email=profile.email,
        role=profile.role,
        active=profile.active,
        verified=profile.verified,
        specialty=profile.specialty,
    )


async def require_staff(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    """Exige rol de staff activo y verificado (doctor/specialist/admin/super_admin)."""
    if not principal.is_staff:
        logger.warning(
            "SEC:forbidden user_id=%s role=%s active=%s",
            principal.id,
            principal.role,
            principal.active,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requiere permisos de personal médico/administrativo activo.",
        )
    return principal


async def require_admin(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    """Exige rol de administrador activo (admin/super_admin)."""
    if not principal.is_admin:
        logger.warning(
            "SEC:forbidden user_id=%s role=%s active=%s",
            principal.id,
            principal.role,
            principal.active,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requiere permisos de administrador.",
        )
    return principal
