"""Pruebas del endpoint de lectura del audit_log."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


async def test_audit_log_muestra_asignaciones(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = make_profile(role="patient")
    db_session.add(user)
    await db_session.flush()
    await client.post(f"{PREFIX}/users/{user.id}/roles", json={"role_code": "doctor"})

    resp = await client.get(f"{PREFIX}/audit-log", params={"action": "role.assigned"})
    assert resp.status_code == 200
    entry = next(e for e in resp.json() if e["resource_id"] == str(user.id))
    assert entry["action"] == "role.assigned"
    assert entry["metadata"]["role"] == "doctor"  # se expone como "metadata"


async def test_audit_log_requiere_permiso(client: AsyncClient, db_session: AsyncSession) -> None:
    doctor = make_profile(role="doctor")  # doctor no tiene 'audit.read'
    db_session.add(doctor)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/audit-log", headers=auth_headers(doctor.id))
    assert resp.status_code == 403


async def test_audit_log_sin_token_401(live_client: AsyncClient) -> None:
    resp = await live_client.get(f"{PREFIX}/audit-log")
    assert resp.status_code == 401
