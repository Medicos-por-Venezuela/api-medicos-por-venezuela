"""Registro de DEV (solo local): crea el usuario en la BD sin pasar por Supabase Auth.

Los triggers de la BD (user_roles y doctors) se disparan solos al crear el usuario.
En producción NO se usa: el signup lo maneja Supabase Auth.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import UnprocessableError
from src.models.profile import Profile

_ALLOWED_ROLES = {"patient", "doctor"}  # nunca admin/super_admin desde el cliente


async def register_or_get(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    role: str,
    specialty: str | None = None,
    whatsapp_number: str | None = None,
    country: str | None = None,
    medical_license: str | None = None,
) -> tuple[Profile, bool]:
    """Crea el usuario (o devuelve el existente por email). Retorna (profile, creado)."""
    if role not in _ALLOWED_ROLES:
        raise UnprocessableError(f"role debe ser uno de {sorted(_ALLOWED_ROLES)}")

    existing = (
        await session.execute(select(Profile).where(Profile.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    profile = Profile(
        id=uuid.uuid4(),
        email=email,
        full_name=full_name,
        role=role,
        specialty=specialty,
        whatsapp_number=whatsapp_number,
        country=country,
        medical_license=medical_license,
        active=True,
        verified=True,
        role_chosen=True,
    )
    session.add(profile)
    await session.commit()  # dispara el trigger de user_roles
    await session.refresh(profile)
    return profile, True


async def get_by_email(session: AsyncSession, email: str) -> Profile | None:
    """Busca la cuenta por email (para el login de DEV)."""
    return (
        await session.execute(select(Profile).where(Profile.email == email))
    ).scalar_one_or_none()
