"""Pruebas del recurso affected_zones (CRUD admin), incluye auditoría."""

import uuid

from httpx import AsyncClient

from src.models.profile import Profile

PREFIX = "/api/v1"


def _payload(**overrides: object) -> dict:
    base = {"name": f"Zona {uuid.uuid4()}", "state": "Miranda"}
    base.update(overrides)
    return base


async def test_affected_zone_crud_flow(client: AsyncClient, admin_identity: Profile) -> None:
    payload = _payload()
    created = await client.post(f"{PREFIX}/affected-zones", json=payload)
    assert created.status_code == 201, created.text
    zone_id = created.json()["id"]

    patched = await client.patch(f"{PREFIX}/affected-zones/{zone_id}", json={"status": "inactive"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "inactive"

    deleted = await client.delete(f"{PREFIX}/affected-zones/{zone_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"{PREFIX}/affected-zones/{zone_id}")).status_code == 404

    audit_resp = await client.get(f"{PREFIX}/audit-log", params={"resource": "affected_zones"})
    entries = [e for e in audit_resp.json() if e["resource_id"] == zone_id]
    assert sorted(e["action"] for e in entries) == sorted(
        ["catalog.created", "catalog.updated", "catalog.deleted"]
    )
    assert all(e["actor_user_id"] == str(admin_identity.id) for e in entries)


async def test_affected_zone_not_found(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"{PREFIX}/affected-zones/{missing}")).status_code == 404
    assert (
        await client.patch(f"{PREFIX}/affected-zones/{missing}", json={"status": "inactive"})
    ).status_code == 404
    assert (await client.delete(f"{PREFIX}/affected-zones/{missing}")).status_code == 404
