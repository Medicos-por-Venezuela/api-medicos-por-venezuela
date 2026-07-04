"""Pruebas del RBAC: carga de roles/permisos efectivos, multi-rol y fallback."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.rbac import Role, UserRole
from src.services.authz import load_authz
from tests._helpers import make_profile


async def _role_id(db_session: AsyncSession, code: str):
    return (await db_session.execute(select(Role.id).where(Role.code == code))).scalar_one()


async def _profile(db_session: AsyncSession, role: str = "patient"):
    prof = make_profile(role=role)
    db_session.add(prof)
    await db_session.flush()
    return prof


async def test_authz_desde_user_roles(db_session: AsyncSession) -> None:
    prof = await _profile(db_session)
    db_session.add(UserRole(user_id=prof.id, role_id=await _role_id(db_session, "doctor")))
    await db_session.flush()

    roles, perms = await load_authz(db_session, prof.id, prof.role)
    assert "doctor" in roles
    assert "consultations.read" in perms
    assert "patients.delete" not in perms  # doctor no tiene delete


async def test_authz_multi_rol_une_permisos(db_session: AsyncSession) -> None:
    """El corazón del multi-rol: un usuario con doctor + admin suma los permisos."""
    prof = await _profile(db_session)
    db_session.add(UserRole(user_id=prof.id, role_id=await _role_id(db_session, "doctor")))
    db_session.add(UserRole(user_id=prof.id, role_id=await _role_id(db_session, "admin")))
    await db_session.flush()

    roles, perms = await load_authz(db_session, prof.id, prof.role)
    assert {"doctor", "admin"} <= roles
    assert "consultations.read" in perms  # de doctor
    assert "patients.delete" in perms  # de admin
    assert "roles.assign" in perms  # de admin


async def test_authz_fallback_a_profiles_role(db_session: AsyncSession) -> None:
    prof = await _profile(db_session, role="doctor")  # sin user_roles -> fallback
    roles, perms = await load_authz(db_session, prof.id, prof.role)
    assert roles == frozenset({"doctor"})
    assert "consultations.read" in perms


async def test_authz_fallback_specialist_es_doctor(db_session: AsyncSession) -> None:
    prof = await _profile(db_session, role="specialist")
    roles, _ = await load_authz(db_session, prof.id, prof.role)
    assert roles == frozenset({"doctor"})


async def test_authz_ignora_roles_revocados(db_session: AsyncSession) -> None:
    prof = await _profile(db_session)
    db_session.add(
        UserRole(
            user_id=prof.id,
            role_id=await _role_id(db_session, "admin"),
            revoked_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    roles, perms = await load_authz(db_session, prof.id, prof.role)
    # El único rol asignado está revocado -> cae al fallback (patient, sin permisos).
    assert "admin" not in roles
    assert "roles.assign" not in perms
