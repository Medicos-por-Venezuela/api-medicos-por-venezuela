"""Pruebas del recurso profiles (solo lectura; usa datos ya restaurados)."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests._helpers import make_profile

PREFIX = "/api/v1"


async def test_list_and_get_profile(client: AsyncClient) -> None:
    listed = await client.get(f"{PREFIX}/profiles", params={"limit": 1})
    assert listed.status_code == 200
    body = listed.json()
    # Respuesta paginada: {items, total}. El total cuenta TODO (no solo la página de limit=1).
    assert len(body["items"]) == 1
    assert body["total"] >= 1
    profile_id = body["items"][0]["id"]

    got = await client.get(f"{PREFIX}/profiles/{profile_id}")
    assert got.status_code == 200
    assert got.json()["id"] == profile_id


async def test_list_profiles_filter_role(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/profiles", params={"role": "doctor", "limit": 5})
    assert resp.status_code == 200
    assert all(p["role"] == "doctor" for p in resp.json()["items"])


async def test_list_profiles_multi_role_active_and_total(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    activo = make_profile(role="specialist")
    revocado = make_profile(role="admin")
    revocado.active = False
    db_session.add_all([activo, revocado])
    await db_session.flush()

    # roles múltiples: specialist + admin.
    resp = await client.get(
        f"{PREFIX}/profiles", params=[("roles", "specialist"), ("roles", "admin"), ("limit", 100)]
    )
    assert resp.status_code == 200
    body = resp.json()
    got = {p["id"] for p in body["items"]}
    assert str(activo.id) in got and str(revocado.id) in got
    assert body["total"] >= len(body["items"])

    # active=false → solo revocados.
    only_revoked = await client.get(f"{PREFIX}/profiles", params={"active": "false", "limit": 100})
    ids = {p["id"] for p in only_revoked.json()["items"]}
    assert str(revocado.id) in ids and str(activo.id) not in ids


async def test_list_profiles_search_by_name(client: AsyncClient, db_session: AsyncSession) -> None:
    hit = make_profile(role="doctor")
    hit.full_name = "Zoraida Buscada Perez"
    miss = make_profile(role="doctor")
    miss.full_name = "Otro Distinto"
    db_session.add_all([hit, miss])
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/profiles", params={"search": "oraida", "limit": 100})
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()["items"]}
    assert str(hit.id) in ids
    assert str(miss.id) not in ids


async def test_list_profiles_search_by_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = make_profile(role="doctor")
    doc.email = "buscame.unico@example.com"
    db_session.add(doc)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/profiles", params={"search": "buscame.unico", "limit": 100})
    assert resp.status_code == 200
    assert str(doc.id) in {p["id"] for p in resp.json()["items"]}


async def test_profile_not_found(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/profiles/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
