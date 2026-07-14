"""Pruebas del recurso profiles (solo lectura; usa datos ya restaurados)."""

from httpx import AsyncClient

PREFIX = "/api/v1"


async def test_list_and_get_profile(client: AsyncClient) -> None:
    listed = await client.get(f"{PREFIX}/profiles", params={"limit": 1})
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) >= 1
    profile_id = rows[0]["id"]

    got = await client.get(f"{PREFIX}/profiles/{profile_id}")
    assert got.status_code == 200
    assert got.json()["id"] == profile_id


async def test_list_profiles_filter_role(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/profiles", params={"role": "doctor", "limit": 5})
    assert resp.status_code == 200
    assert all(p["role"] == "doctor" for p in resp.json())


async def test_profile_not_found(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/profiles/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
