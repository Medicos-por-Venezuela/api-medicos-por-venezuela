"""Pruebas de los endpoints de gestión de roles (RBAC): asignar/revocar + auditoría."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit_log import AuditLog
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
