"""El trigger de coexistencia refleja profiles.role -> user_roles al crear el perfil."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.rbac import Role, UserRole
from tests._helpers import make_profile


async def _active_role_codes(db_session: AsyncSession, user_id) -> set[str]:
    rows = await db_session.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, UserRole.revoked_at.is_(None))
    )
    return set(rows.scalars().all())


async def test_trigger_espeja_rol_al_crear_perfil(db_session: AsyncSession) -> None:
    prof = make_profile(role="doctor")
    db_session.add(prof)
    await db_session.flush()  # dispara el trigger AFTER INSERT

    assert "doctor" in await _active_role_codes(db_session, prof.id)


async def test_trigger_mapea_specialist_a_doctor(db_session: AsyncSession) -> None:
    prof = make_profile(role="specialist")  # legacy -> doctor
    db_session.add(prof)
    await db_session.flush()

    codes = await _active_role_codes(db_session, prof.id)
    assert codes == {"doctor"}


async def test_trigger_no_duplica_rol_activo(db_session: AsyncSession) -> None:
    """Reasignar el mismo rol (UPDATE OF role al mismo valor) no crea filas duplicadas."""
    prof = make_profile(role="doctor")
    db_session.add(prof)
    await db_session.flush()

    prof.role = "doctor"  # UPDATE OF role -> vuelve a disparar; debe ser no-op
    await db_session.flush()

    rows = await db_session.execute(
        select(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id == prof.id,
            UserRole.revoked_at.is_(None),
            Role.code == "doctor",
        )
    )
    assert len(rows.scalars().all()) == 1
