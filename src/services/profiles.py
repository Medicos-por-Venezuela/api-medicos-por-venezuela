"""Capa de negocio para profiles (staff)."""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import BadRequestError, NotFoundError
from src.models.profile import Profile
from src.services import audit

# set_my_role solo permite finalizar como paciente o médico (nunca escalar).
_SELF_ROLES = {"patient", "doctor"}


async def list_profiles(
    session: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    role: str | None = None,
    search: str | None = None,
) -> list[Profile]:
    stmt = select(Profile)
    if role:
        stmt = stmt.where(Profile.role == role)
    # Búsqueda server-side por nombre o email (con ~3000 usuarios, paginar sin buscar es
    # inservible). ilike va como parámetro enlazado -> sin riesgo de inyección.
    if search and (term := search.strip()):
        like = f"%{term}%"
        stmt = stmt.where(or_(Profile.full_name.ilike(like), Profile.email.ilike(like)))
    stmt = stmt.order_by(Profile.created_at.desc()).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_profile(session: AsyncSession, profile_id: uuid.UUID) -> Profile:
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise NotFoundError("Perfil no encontrado.")
    return profile


async def set_active(
    session: AsyncSession,
    profile_id: uuid.UUID,
    active: bool,
    actor_user_id: uuid.UUID | None = None,
) -> Profile:
    """Revoca (`active=false`) o reactiva (`active=true`) un médico. Acción de admin."""
    profile = await get_profile(session, profile_id)
    profile.active = active
    await audit.log_action(
        session,
        action="profile.activated" if active else "profile.deactivated",
        actor_user_id=actor_user_id,
        resource="users",
        resource_id=profile_id,
    )
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
    await audit.log_action(
        session,
        action="profile.role_chosen",
        actor_user_id=profile_id,
        resource="users",
        resource_id=profile_id,
        metadata={"role": role},
    )
    await session.commit()
    await session.refresh(profile)
    return profile
