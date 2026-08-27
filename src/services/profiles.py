"""Capa de negocio para profiles (staff)."""

import uuid
from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import BadRequestError, ForbiddenError, NotFoundError
from src.models.profile import Profile
from src.services import audit

# set_my_role solo permite finalizar como paciente o médico (nunca escalar).
_SELF_ROLES = {"patient", "doctor"}


async def list_profiles(
    session: AsyncSession,
    *,
    skip: int = 0,
    limit: int = 100,
    role: str | None = None,
    roles: list[str] | None = None,
    search: str | None = None,
    active: bool | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
) -> tuple[list[Profile], int]:
    """Perfiles filtrados + total exacto (para la tabla de médicos/usuarios del admin). Reemplaza
    el acceso directo del frontend a `users`. Filtros: uno o varios roles, estado activo/revocado,
    rango de fechas, y búsqueda por nombre/email/especialidad. Todo con parámetros enlazados."""
    conditions = []
    if roles:
        conditions.append(Profile.role.in_(roles))
    elif role:
        conditions.append(Profile.role == role)
    # Búsqueda server-side por nombre, email o especialidad (con ~3000 usuarios, paginar sin buscar
    # es inservible). ilike va como parámetro enlazado -> sin riesgo de inyección.
    if search and (term := search.strip()):
        like = f"%{term}%"
        conditions.append(
            or_(
                Profile.full_name.ilike(like),
                Profile.email.ilike(like),
                Profile.specialty.ilike(like),
            )
        )
    if active is not None:
        conditions.append(Profile.active.is_(active))
    if created_from is not None:
        conditions.append(Profile.created_at >= created_from)
    if created_to is not None:
        # Incluir todo el día `created_to`: created_at < día siguiente.
        conditions.append(Profile.created_at < created_to + timedelta(days=1))

    base = select(Profile)
    if conditions:
        base = base.where(*conditions)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.order_by(Profile.created_at.desc()).offset(skip).limit(limit)
    items = list((await session.execute(stmt)).scalars().all())
    return items, total


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
    solo `patient`/`doctor`, solo si `role_chosen` aún es false.

    ⚠️ Elegir rol NO concede acceso: este servicio **nunca** toca `active`/`verified`.
    Las cuentas nacen con ambos en true (trigger `handle_new_auth_user`), así que fijarlos
    aquí era redundante y permitía a una cuenta revocada (`active=false`, típica de un alta
    OAuth sin rol a la que un admin le quitó el acceso) reactivarse sola con una sola
    petición, anulando la revocación. Además, una cuenta revocada no finaliza nada: 403.
    """
    if role not in _SELF_ROLES:
        raise BadRequestError("Rol inválido. Solo se permite 'patient' o 'doctor'.")
    profile = await get_profile(session, profile_id)
    if not profile.active:
        raise ForbiddenError("Cuenta revocada.")
    if profile.role_chosen:
        raise BadRequestError("El rol ya fue elegido y no puede cambiarse.")

    profile.role = role
    if role == "doctor":
        profile.specialty = specialty
        profile.country = country
        profile.medical_license = medical_license
        profile.whatsapp_number = whatsapp_number
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
