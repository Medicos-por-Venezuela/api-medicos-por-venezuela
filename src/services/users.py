"""Creación administrativa de usuarios de Supabase Auth (Admin API).

Flujo (ver design.md "Data Flow"): [0] bloquea `super_admin` como rol inicial
antes de cualquier llamada de red; [1] lookup idempotente por email (evita
duplicar la cuenta de Auth en un reintento); [2] creación vía Admin API con el
service-role key — el trigger `handle_new_auth_user()` crea `public.users` en
la MISMA transacción de Auth, así que la fila ya está commiteada cuando la
Admin API responde (sin polling); [3] `user.created` se audita y commitea de
inmediato (hecho durable, no se revierte aunque el paso [4] falle); [4] si se
pidió `initial_role`, se delega en el `assign_role` ya existente (propio commit,
409/422-safe).

El service-role key SOLO se usa aquí (nunca se loguea, nunca en `sacs.py` ni en
ningún otro módulo): ver `.claude/rules/security.md`.
"""

import logging
import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.errors import ConflictError, UnprocessableError, UpstreamServiceError
from src.models.profile import Profile
from src.schemas.user import UserCreate
from src.services import audit
from src.services import user_roles as user_roles_service

logger = logging.getLogger("mpv.api")

_ADMIN_USERS_PATH = "/auth/v1/admin/users"
# Corto a propósito: este endpoint retiene una conexión del pool de Postgres
# (ya tomada por `require_permission` antes de llegar acá) durante las llamadas
# a la Admin API. Con hasta 2 llamadas secuenciales, un timeout largo puede
# agotar el pool compartido con la cola (`nowait=True`, fail-fast) si Supabase
# Auth está lento. Supabase Auth normalmente responde en <1s.
_TIMEOUT = 5.0


def _admin_headers() -> dict[str, str]:
    key = settings.SUPABASE_SERVICE_ROLE_KEY
    return {"Authorization": f"Bearer {key}", "apikey": key}


async def _lookup_user_by_email(email: str) -> dict | None:
    """Busca un usuario de Auth por email vía la Admin API. `None` si no existe.

    Nunca loguea el body de la respuesta ni el service-role key: solo el
    status/tipo de excepción (mismo patrón que `services/sacs.py`).
    """
    url = f"{settings.SUPABASE_URL}{_ADMIN_USERS_PATH}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url, params={"email": email}, headers=_admin_headers())
            response.raise_for_status()
            users = response.json().get("users", [])
    except httpx.HTTPStatusError as exc:
        logger.error("Supabase Admin API lookup error status=%s", exc.response.status_code)
        raise UpstreamServiceError("Fallo al consultar el proveedor de autenticación.") from exc
    except httpx.RequestError as exc:
        logger.error("Supabase Admin API lookup connection error type=%s", type(exc).__name__)
        raise UpstreamServiceError("Fallo al consultar el proveedor de autenticación.") from exc
    except (ValueError, TypeError, AttributeError) as exc:
        # Respuesta 2xx pero con body inesperado/corrupto: no debe escapar como 500.
        logger.error("Supabase Admin API lookup malformed response type=%s", type(exc).__name__)
        raise UpstreamServiceError("Fallo al consultar el proveedor de autenticación.") from exc

    return users[0] if users else None


async def _create_auth_user(email: str, password: str, full_name: str) -> dict:
    """Crea el usuario de Auth vía la Admin API (el trigger crea `public.users`)."""
    url = f"{settings.SUPABASE_URL}{_ADMIN_USERS_PATH}"
    body = {
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": {"full_name": full_name},
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json=body, headers=_admin_headers())
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        # 400/422 de la Admin API en `create` son casi siempre "el email ya existe"
        # (carrera contra el lookup idempotente, no una falla del proveedor):
        # reportar 409, no 502, para no ocultar un conflicto real como caída externa.
        if exc.response.status_code in (400, 422):
            logger.error("Supabase Admin API create rechazó email duplicado (race)")
            raise ConflictError("Ya existe un usuario con ese correo electrónico.") from exc
        logger.error("Supabase Admin API create error status=%s", exc.response.status_code)
        raise UpstreamServiceError(
            "Fallo al crear el usuario en el proveedor de autenticación."
        ) from exc
    except httpx.RequestError as exc:
        logger.error("Supabase Admin API create connection error type=%s", type(exc).__name__)
        raise UpstreamServiceError(
            "Fallo al crear el usuario en el proveedor de autenticación."
        ) from exc
    except (ValueError, TypeError, AttributeError) as exc:
        # Respuesta 2xx pero con body inesperado/corrupto: no debe escapar como 500.
        logger.error("Supabase Admin API create malformed response type=%s", type(exc).__name__)
        raise UpstreamServiceError(
            "Fallo al crear el usuario en el proveedor de autenticación."
        ) from exc


async def create_user(
    session: AsyncSession,
    payload: UserCreate,
    actor_user_id: uuid.UUID,
    actor_roles: frozenset[str],
) -> tuple[Profile, str]:
    """Crea un usuario de Auth (+ perfil vía trigger) y, opcionalmente, un rol inicial.

    Devuelve `(profile, effective_role)`: `profile.role` es el valor legado que el
    trigger `handle_new_auth_user()` resuelve (siempre 'patient' para altas vía Admin
    API, ya que no le mandamos `role` en `user_metadata`); `effective_role` es el rol
    RBAC realmente otorgado (`payload.initial_role` si se pidió y `assign_role` no
    falló, si no el de `profile`) — es el que debe mostrarse en la respuesta.

    `super_admin` como `initial_role` se rechaza aquí (422) para TODO actor, incluso
    uno que ya sea `super_admin`: esta restricción de creación es independiente del
    guard de actor dentro de `assign_role` (ver services/user_roles.py) — ambas
    restricciones son ortogonales y ninguna sustituye a la otra (spec:
    "Creation-time super_admin block and actor-guard are independent").
    """
    if payload.initial_role == "super_admin":
        raise UnprocessableError(
            "No se puede otorgar 'super_admin' al crear un usuario. "
            "Usa POST /users/{id}/roles con un actor que ya sea super_admin."
        )

    if await _lookup_user_by_email(payload.email) is not None:
        raise ConflictError("Ya existe un usuario con ese correo electrónico.")

    auth_user = await _create_auth_user(payload.email, payload.password, payload.full_name)
    try:
        user_id = uuid.UUID(str(auth_user["id"]))
    except (KeyError, ValueError, TypeError) as exc:
        logger.error("Supabase Admin API create response missing/invalid id field")
        raise UpstreamServiceError(
            "El proveedor de autenticación devolvió una respuesta inesperada."
        ) from exc

    profile = await session.get(Profile, user_id)
    if profile is None:
        # No debería ocurrir (el trigger corre síncrono en la misma transacción de
        # Auth), pero si pasa es un fallo del proveedor, no un 500 genérico.
        raise UpstreamServiceError(
            "El usuario se creó en el proveedor de autenticación pero el perfil "
            "todavía no está disponible."
        )

    await audit.log_action(
        session,
        action="user.created",
        actor_user_id=actor_user_id,
        resource="users",
        resource_id=user_id,
        metadata={"email": payload.email},
    )
    await session.commit()
    await session.refresh(profile)

    if payload.initial_role:
        # Falla propagable (422/409): el usuario de Auth ya creado NO se revierte
        # (spec "Partial-failure safety"); un reintento con el mismo email es
        # idempotente (ver `_lookup_user_by_email` -> ConflictError).
        await user_roles_service.assign_role(
            session,
            user_id,
            payload.initial_role,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
        )

    return profile, payload.initial_role or profile.role
