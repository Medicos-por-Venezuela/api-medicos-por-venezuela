"""Pruebas de los endpoints de gestión de roles (RBAC): asignar/revocar + auditoría."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit_log import AuditLog
from src.models.profile import Profile
from src.models.rbac import Role, UserRole
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


async def _target_user(db_session: AsyncSession):
    prof = make_profile(role="patient")
    db_session.add(prof)
    await db_session.flush()
    return prof


async def test_catalogo_de_roles(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/roles")
    assert resp.status_code == 200
    codes = {r["code"] for r in resp.json()}
    assert {"patient", "doctor", "admin", "super_admin"} <= codes


async def test_asignar_listar_revocar(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _target_user(db_session)

    resp = await client.post(f"{PREFIX}/users/{user.id}/roles", json={"role_code": "doctor"})
    assert resp.status_code == 201, resp.text
    role_id = resp.json()["role_id"]

    listed = await client.get(f"{PREFIX}/users/{user.id}/roles")
    assert any(r["role_code"] == "doctor" for r in listed.json())

    dup = await client.post(f"{PREFIX}/users/{user.id}/roles", json={"role_code": "doctor"})
    assert dup.status_code == 409

    rev = await client.delete(f"{PREFIX}/users/{user.id}/roles/{role_id}")
    assert rev.status_code == 204

    listed2 = await client.get(f"{PREFIX}/users/{user.id}/roles")
    assert not any(r["role_code"] == "doctor" for r in listed2.json())


async def test_asignar_registra_audit(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _target_user(db_session)
    await client.post(f"{PREFIX}/users/{user.id}/roles", json={"role_code": "doctor"})

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "role.assigned", AuditLog.resource_id == str(user.id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].metadata_["role"] == "doctor"


async def test_rol_inexistente_422(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _target_user(db_session)
    resp = await client.post(f"{PREFIX}/users/{user.id}/roles", json={"role_code": "nope"})
    assert resp.status_code == 422


async def test_usuario_inexistente_404(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    resp = await client.post(f"{PREFIX}/users/{missing}/roles", json={"role_code": "doctor"})
    assert resp.status_code == 404


async def test_sin_permiso_roles_assign_403(client: AsyncClient, db_session: AsyncSession) -> None:
    doctor = make_profile(role="doctor")  # doctor no tiene 'roles.assign'
    db_session.add(doctor)
    await db_session.flush()
    target = await _target_user(db_session)

    resp = await client.post(
        f"{PREFIX}/users/{target.id}/roles",
        json={"role_code": "doctor"},
        headers=auth_headers(doctor.id),
    )
    assert resp.status_code == 403


async def test_super_admin_actor_otorga_super_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un actor que YA es super_admin puede otorgar super_admin (201, auditado)."""
    super_admin = Profile(
        id=uuid.uuid4(),
        full_name="Test Super Admin",
        role="super_admin",
        active=True,
        verified=True,
        role_chosen=True,
    )
    db_session.add(super_admin)
    await db_session.flush()
    target = await _target_user(db_session)

    resp = await client.post(
        f"{PREFIX}/users/{target.id}/roles",
        json={"role_code": "super_admin"},
        headers=auth_headers(super_admin.id),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role_code"] == "super_admin"

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "role.assigned", AuditLog.resource_id == str(target.id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].metadata_["role"] == "super_admin"


async def test_admin_plano_no_puede_otorgar_super_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un `admin` plano (con `roles.assign` pero sin `super_admin`) recibe 403.

    El `client` de la fixture ya autentica como un `admin` legado (no super_admin):
    esto ejerce exactamente ese caso, sin crear un perfil adicional.
    """
    target = await _target_user(db_session)

    resp = await client.post(
        f"{PREFIX}/users/{target.id}/roles",
        json={"role_code": "super_admin"},
    )
    assert resp.status_code == 403

    rows = (
        (
            await db_session.execute(
                select(UserRole)
                .join(Role, Role.id == UserRole.role_id)
                .where(UserRole.user_id == target.id, Role.code == "super_admin")
            )
        )
        .scalars()
        .all()
    )
    assert rows == []

    audit_rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "role.assigned", AuditLog.resource_id == str(target.id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert audit_rows == []


async def test_super_admin_actor_revoca_super_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un actor que YA es super_admin puede revocar el rol super_admin de otro (204)."""
    super_admin = Profile(
        id=uuid.uuid4(),
        full_name="Test Super Admin",
        role="super_admin",
        active=True,
        verified=True,
        role_chosen=True,
    )
    db_session.add(super_admin)
    target = await _target_user(db_session)
    super_admin_role = (
        await db_session.execute(select(Role).where(Role.code == "super_admin"))
    ).scalar_one()
    target_grant = UserRole(
        user_id=target.id, role_id=super_admin_role.id, assigned_by=super_admin.id
    )
    db_session.add(target_grant)
    await db_session.flush()

    resp = await client.delete(
        f"{PREFIX}/users/{target.id}/roles/{super_admin_role.id}",
        headers=auth_headers(super_admin.id),
    )
    assert resp.status_code == 204, resp.text

    listed = await client.get(
        f"{PREFIX}/users/{target.id}/roles", headers=auth_headers(super_admin.id)
    )
    assert not any(r["role_code"] == "super_admin" for r in listed.json())


async def test_admin_plano_no_puede_revocar_super_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un `admin` plano NO puede revocar el rol super_admin de nadie (403, sin cambios)."""
    target = await _target_user(db_session)
    super_admin_role = (
        await db_session.execute(select(Role).where(Role.code == "super_admin"))
    ).scalar_one()
    target_grant = UserRole(user_id=target.id, role_id=super_admin_role.id, assigned_by=target.id)
    db_session.add(target_grant)
    await db_session.flush()

    resp = await client.delete(f"{PREFIX}/users/{target.id}/roles/{super_admin_role.id}")
    assert resp.status_code == 403

    listed = await client.get(f"{PREFIX}/users/{target.id}/roles")
    assert any(r["role_code"] == "super_admin" for r in listed.json())

    audit_rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "role.revoked", AuditLog.resource_id == str(target.id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert audit_rows == []
