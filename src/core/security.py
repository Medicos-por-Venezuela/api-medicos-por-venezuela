"""Autenticación (valida el JWT de Supabase) y autorización (RBAC).

El login sigue en Supabase Auth: el frontend obtiene un JWT y lo envía como
`Authorization: Bearer <token>`. Aquí validamos firma/exp/audiencia, extraemos el
`sub` (= id del perfil) y cargamos el rol desde `profiles`. Las decisiones de rol
replican las políticas RLS (is_staff / is_admin).
"""

import logging
import uuid
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.db.session import get_db
from src.models.profile import Profile
from src.services import authz

logger = logging.getLogger("mpv.api")

# Roles RBAC con acceso de staff / admin.
STAFF_ROLES = {"doctor", "admin", "super_admin"}
ADMIN_ROLES = {"admin", "super_admin"}

# Prioridad para colapsar el multi-rol a UN rol "efectivo" (el más alto gana): lo usa
# /auth/me para que un dual doctor+super_admin se presente como super_admin, aunque el
# `users.role` legado diga otra cosa. La fuente de verdad es user_roles, no esa columna.
_ROLE_PRIORITY = ("super_admin", "admin", "doctor", "specialist", "patient")


def effective_role(roles: frozenset[str]) -> str | None:
    """El rol de mayor prioridad del set RBAC efectivo, o None si el set está vacío."""
    return next((r for r in _ROLE_PRIORITY if r in roles), None)

# auto_error=False: gestionamos nosotros el 401 (mensaje uniforme).
_bearer = HTTPBearer(auto_error=False)


class Principal(BaseModel):
    """Identidad autenticada: JWT + perfil + roles/permisos RBAC efectivos."""

    id: uuid.UUID
    email: str | None = None
    role: str  # profiles.role (legado; se conserva por compatibilidad)
    active: bool
    verified: bool
    specialty: str | None = None
    roles: frozenset[str] = frozenset()  # roles RBAC efectivos (user_roles o fallback)
    permissions: frozenset[str] = frozenset()  # permisos efectivos (unión de sus roles)

    @property
    def is_staff(self) -> bool:
        return bool(self.roles & STAFF_ROLES) and self.active and self.verified

    @property
    def is_admin(self) -> bool:
        return bool(self.roles & ADMIN_ROLES) and self.active

    def has_permission(self, code: str) -> bool:
        # Un usuario revocado (active=false) pierde TODOS los permisos al instante.
        return self.active and code in self.permissions


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


# Algoritmos asimétricos de las "JWT signing keys" de Supabase (rotables, vía JWKS).
# HS256 (secreto compartido) sigue siendo el esquema legacy de la mayoría de proyectos.
_ASYMMETRIC_ALGORITHMS = ("ES256", "RS256", "PS256")


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    """Cliente JWKS cacheado por URL (PyJWKClient ya cachea las claves por `kid`)."""
    return jwt.PyJWKClient(jwks_url)


def decode_token(token: str) -> dict:
    """Valida y decodifica el JWT de Supabase. Lanza 401 si es inválido/expirado.

    Soporta dos esquemas de firma (Supabase los usa según el proyecto):
      - HS256 con secreto compartido (SUPABASE_JWT_SECRET) — el legacy, la mayoría hoy.
      - Asimétrico vía JWKS (SUPABASE_JWKS_URL) — las "JWT signing keys" nuevas de
        Supabase (rotables); el CLI de Supabase LOCAL las usa por defecto.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        logger.warning("SEC:token_invalid reason=%s", type(exc).__name__)
        raise _unauthorized("Token inválido o expirado.") from exc

    # PyJWT solo verifica `iss` si se le pasa el parámetro: sin SUPABASE_JWT_ISSUER definido
    # el comportamiento es el de siempre (el Supabase local emite otro iss y rompería dev).
    issuer = {"issuer": settings.SUPABASE_JWT_ISSUER} if settings.SUPABASE_JWT_ISSUER else {}
    try:
        if header.get("alg") in _ASYMMETRIC_ALGORITHMS and settings.SUPABASE_JWKS_URL:
            signing_key = _jwks_client(settings.SUPABASE_JWKS_URL).get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=list(_ASYMMETRIC_ALGORITHMS),
                audience=settings.SUPABASE_JWT_AUDIENCE,
                **issuer,
            )
        return jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=[settings.SUPABASE_JWT_ALGORITHM],
            audience=settings.SUPABASE_JWT_AUDIENCE,
            **issuer,
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
    roles, permissions = await authz.load_authz(db, profile.id, profile.role)
    return Principal(
        id=profile.id,
        email=profile.email,
        role=profile.role,
        active=profile.active,
        verified=profile.verified,
        specialty=profile.specialty,
        roles=roles,
        permissions=permissions,
    )


async def get_optional_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Principal | None:
    """Como `get_current_principal`, pero devuelve None en vez de 401 si no hay sesión.

    Para endpoints que sirven a DOS clientes: el paciente anónimo (que se identifica con otra
    credencial) y el staff autenticado. Un token presente pero inválido sigue siendo 401 — que
    no haya sesión es distinto de traer una rota."""
    if credentials is None:
        return None
    return await get_current_principal(credentials, db)


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


def require_permission(code: str):
    """Fábrica de dependencia RBAC granular: exige el permiso `code`.

    Uso:  _: Principal = Depends(require_permission("doctors.verify"))
    """

    async def _require(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not principal.has_permission(code):
            logger.warning("SEC:forbidden user_id=%s missing_perm=%s", principal.id, code)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para esta acción.",
            )
        return principal

    return _require
