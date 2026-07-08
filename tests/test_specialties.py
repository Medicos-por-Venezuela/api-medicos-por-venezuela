"""Tests for specialty matching and CRUD."""

import uuid
from collections.abc import AsyncGenerator

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_db
from src.main import app
from src.models.profile import Profile
from src.services.specialties import can_attend, compute_priority, matches_specialty
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


# --- matches_specialty ---


def test_matches_general_is_wildcard() -> None:
    assert matches_specialty("Medicina general", None, ["Lesión física"]) is True


def test_matches_by_need() -> None:
    assert matches_specialty("Psicología", None, ["Apoyo emocional"]) is True
    assert matches_specialty("Traumatología", None, ["Lesión física"]) is True


def test_matches_false_when_unrelated() -> None:
    assert matches_specialty("Cardiología", None, ["Lesión física"]) is False
    assert matches_specialty(None, None, ["x"]) is False


# --- can_attend ---


def test_reserved_need_blocks_general_doctor() -> None:
    assert can_attend("Medicina general", None, ["Crisis de ansiedad"]) is False
    assert can_attend("Psiquiatría", None, ["Crisis de ansiedad"]) is True


def test_psychology_only_takes_psych_cases() -> None:
    assert can_attend("Psicología", None, ["Lesión física"]) is False
    assert can_attend("Psicología", None, ["Apoyo emocional"]) is True


def test_can_attend_physical_general() -> None:
    assert can_attend("Medicina general", None, ["Lesión física"]) is True


# --- compute_priority ---


def test_priority_review_for_sensitive_tags() -> None:
    assert compute_priority(["Embarazo"]) == "review"
    assert compute_priority(["Niño / pediatría"]) == "review"
    assert compute_priority(["Lesión física"]) == "review"
    assert compute_priority(["Medicina general"]) == "normal"
    assert compute_priority(None) == "normal"


async def _public_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


# --- public endpoints ---


async def test_specialties_list_is_public(db_session: AsyncSession) -> None:
    async for public_client in _public_client(db_session):
        resp = await public_client.get(f"{PREFIX}/specialties")

    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert "Medicina general" in names


async def test_specialties_catalog_endpoint(db_session: AsyncSession) -> None:
    async for public_client in _public_client(db_session):
        resp = await public_client.get(f"{PREFIX}/specialties/catalog")

    assert resp.status_code == 200
    body = resp.json()
    assert "Medicina general" in body["specialties"]
    assert "Apoyo emocional" in body["reserved_needs"]
    assert body["specialty_needs"]["Medicina general"] == ["*"]


# --- CRUD specialties ---


def _payload(**overrides: object) -> dict:
    base = {"name": f"Dermatología {uuid.uuid4()}", "status": "active"}
    base.update(overrides)
    return base


async def test_specialty_crud_flow(client: AsyncClient) -> None:
    payload = _payload()
    created = await client.post(f"{PREFIX}/specialties", json=payload)
    assert created.status_code == 201, created.text
    specialty = created.json()
    specialty_id = specialty["id"]
    assert specialty["name"] == payload["name"].strip()
    assert specialty["status"] == "active"
    assert specialty["deleted_at"] is None

    got = await client.get(f"{PREFIX}/specialties/{specialty_id}")
    assert got.status_code == 200

    listed = await client.get(f"{PREFIX}/specialties")
    assert listed.status_code == 200
    assert any(item["id"] == specialty_id for item in listed.json())

    inactive = await client.post(f"{PREFIX}/specialties", json=_payload(status="inactive"))
    assert inactive.status_code == 201, inactive.text
    listed_after_inactive = await client.get(f"{PREFIX}/specialties")
    assert all(item["id"] != inactive.json()["id"] for item in listed_after_inactive.json())

    # /specialties/admin (catalogs.manage) SÍ ve las inactivas -- la pública nunca las muestra.
    admin_listed = await client.get(f"{PREFIX}/specialties/admin")
    assert admin_listed.status_code == 200
    assert any(item["id"] == inactive.json()["id"] for item in admin_listed.json())

    duplicate = await client.post(f"{PREFIX}/specialties", json=payload)
    assert duplicate.status_code == 409

    patched = await client.patch(
        f"{PREFIX}/specialties/{specialty_id}", json={"name": f"{payload['name']} clínica"}
    )
    assert patched.status_code == 200
    assert patched.json()["name"].endswith("clínica")

    deleted = await client.delete(f"{PREFIX}/specialties/{specialty_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"{PREFIX}/specialties/{specialty_id}")).status_code == 404

    listed_after_delete = await client.get(f"{PREFIX}/specialties")
    assert all(item["id"] != specialty_id for item in listed_after_delete.json())


async def test_specialty_not_found(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"{PREFIX}/specialties/{missing}")).status_code == 404
    assert (
        await client.patch(f"{PREFIX}/specialties/{missing}", json={"status": "inactive"})
    ).status_code == 404
    assert (await client.delete(f"{PREFIX}/specialties/{missing}")).status_code == 404


async def test_specialty_validation_error(client: AsyncClient) -> None:
    assert (await client.post(f"{PREFIX}/specialties", json=_payload(name="A"))).status_code == 422
    assert (await client.post(f"{PREFIX}/specialties", json=_payload(name=123))).status_code == 422
    assert (
        await client.patch(
            f"{PREFIX}/specialties/00000000-0000-0000-0000-000000000000",
            json={"name": 123},
        )
    ).status_code == 422
    assert (
        await client.patch(
            f"{PREFIX}/specialties/00000000-0000-0000-0000-000000000000",
            json={"status": "deleted"},
        )
    ).status_code == 422
    assert (
        await client.patch(
            f"{PREFIX}/specialties/00000000-0000-0000-0000-000000000000",
            json={"status": None},
        )
    ).status_code == 422


async def test_specialty_management_requires_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doctor = make_profile(role="doctor", specialty="Cardiología")
    db_session.add(doctor)
    await db_session.flush()

    headers = auth_headers(doctor.id)
    assert (
        await client.get(
            f"{PREFIX}/specialties/00000000-0000-0000-0000-000000000000", headers=headers
        )
    ).status_code == 403
    assert (
        await client.post(f"{PREFIX}/specialties", json=_payload(), headers=headers)
    ).status_code == 403
    assert (
        await client.patch(
            f"{PREFIX}/specialties/00000000-0000-0000-0000-000000000000",
            json={"status": "inactive"},
            headers=headers,
        )
    ).status_code == 403
    assert (await client.get(f"{PREFIX}/specialties/admin", headers=headers)).status_code == 403
    assert (
        await client.delete(
            f"{PREFIX}/specialties/00000000-0000-0000-0000-000000000000", headers=headers
        )
    ).status_code == 403


async def test_specialty_management_requires_token(db_session: AsyncSession) -> None:
    async for public_client in _public_client(db_session):
        got = await public_client.get(f"{PREFIX}/specialties/00000000-0000-0000-0000-000000000000")
        assert got.status_code == 401
        created = await public_client.post(f"{PREFIX}/specialties", json=_payload())
        assert created.status_code == 401
        assert (
            await public_client.patch(
                f"{PREFIX}/specialties/00000000-0000-0000-0000-000000000000",
                json={"status": "inactive"},
            )
        ).status_code == 401
        assert (
            await public_client.delete(
                f"{PREFIX}/specialties/00000000-0000-0000-0000-000000000000"
            )
        ).status_code == 401


async def test_super_admin_can_manage_specialties(
    client: AsyncClient, db_session: AsyncSession
) -> None:
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

    created = await client.post(
        f"{PREFIX}/specialties", json=_payload(), headers=auth_headers(super_admin.id)
    )
    assert created.status_code == 201, created.text
