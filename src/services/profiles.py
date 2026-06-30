"""Capa de negocio para profiles (staff)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import BadRequestError, NotFoundError
from src.models.profile import Profile

# set_my_role solo permite finalizar como paciente o médico (nunca escalar).
_SELF_ROLES = {"patient", "doctor"}


async def list_profiles(
    session: AsyncSession, skip: int = 0, limit: int = 100, role: str | None = None
) -> list[Profile]:
    stmt = select(Profile)
    if role:
        stmt = stmt.where(Profile.role == role)
    stmt = stmt.order_by(Profile.created_at.desc()).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_profile(session: AsyncSession, profile_id: uuid.UUID) -> Profile:
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise NotFoundError("Perfil no encontrado.")
    return profile


async def mark_online(session: AsyncSession, profile_id: uuid.UUID) -> Profile:
    """Presencia del médico (mark_myself_online): actualiza last_seen_at."""
    profile = await get_profile(session, profile_id)
    profile.last_seen_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(profile)
    return profile


async def set_active(session: AsyncSession, profile_id: uuid.UUID, active: bool) -> Profile:
    """Revoca (`active=false`) o reactiva (`active=true`) un médico. Acción de admin."""
    profile = await get_profile(session, profile_id)
    profile.active = active
    await session.commit()
    await session.refresh(profile)
    return profile


async def finalize_role(
    session: AsyncSession,
    profile_id: uuid.UUID,
    role: str,
    specialty: str | None = None,
    country: str | None = None,
    medical_license: str | None = None,
    whatsapp_number: str | None = None,
) -> Profile:
    """Finaliza el rol del propio usuario una sola vez (réplica de set_my_role):
    solo `patient`/`doctor`, solo si `role_chosen` aún es false."""
    if role not in _SELF_ROLES:
        raise BadRequestError("Rol inválido. Solo se permite 'patient' o 'doctor'.")
    profile = await get_profile(session, profile_id)
    if profile.role_chosen:
        raise BadRequestError("El rol ya fue elegido y no puede cambiarse.")

    profile.role = role
    if role == "doctor":
        profile.specialty = specialty
        profile.country = country
        profile.medical_license = medical_license
        profile.whatsapp_number = whatsapp_number
    profile.verified = True
    profile.active = True
    profile.role_chosen = True
    await session.commit()
    await session.refresh(profile)
    return profile
