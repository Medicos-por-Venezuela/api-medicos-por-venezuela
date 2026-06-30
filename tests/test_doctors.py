"""Pruebas del recurso doctors (CRUD aislado)."""

from httpx import AsyncClient

PREFIX = "/api/v1"


def _payload(**over: object) -> dict:
    base = {
        "full_name": "Dr Prueba",
        "specialty": "Medicina General",
        "country": "Venezuela",
        "phone_whatsapp": "+58412123456",
    }
    base.update(over)
    return base


async def test_doctor_crud_flow(client: AsyncClient) -> None:
    # Create
    resp = await client.post(f"{PREFIX}/doctors", json=_payload())
    assert resp.status_code == 201, resp.text
    doctor = resp.json()
    assert doctor["status"] == "active"
    assert doctor["preferred_platform"] == "google_meet"
    doctor_id = doctor["id"]

    # Get
    got = await client.get(f"{PREFIX}/doctors/{doctor_id}")
    assert got.status_code == 200

    # List (+ filtro por status)
    listed = await client.get(f"{PREFIX}/doctors", params={"status": "active"})
    assert listed.status_code == 200
    assert any(d["id"] == doctor_id for d in listed.json())

    # Patch
    patched = await client.patch(f"{PREFIX}/doctors/{doctor_id}", json={"status": "inactive"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "inactive"

    # Delete
    deleted = await client.delete(f"{PREFIX}/doctors/{doctor_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"{PREFIX}/doctors/{doctor_id}")).status_code == 404


async def test_doctor_not_found(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"{PREFIX}/doctors/{missing}")).status_code == 404
    assert (
        await client.patch(f"{PREFIX}/doctors/{missing}", json={"status": "x"})
    ).status_code == 404
    assert (await client.delete(f"{PREFIX}/doctors/{missing}")).status_code == 404


async def test_doctor_validation_error(client: AsyncClient) -> None:
    resp = await client.post(f"{PREFIX}/doctors", json=_payload(full_name="A"))
    assert resp.status_code == 422
