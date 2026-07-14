"""Tests for the professional_types resource."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, get_current_principal
from src.db.session import get_db
from src.main import app
from src.models.professional_type import ProfessionalType
from src.models.profile import Profile
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


@pytest_asyncio.fixture
async def _professional_types_table(db_session: AsyncSession) -> None:
    await db_session.run_sync(
        lambda session: ProfessionalType.__table__.create(
            bind=session.connection(), checkfirst=True
        )
    )


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, _professional_types_table: None
) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    profile = Profile(
        id=uuid.uuid4(),
        full_name="Test Admin",
        role="admin",
        active=True,
        verified=True,
        role_chosen=True,
    )
    db_session.add(profile)
    await db_session.flush()
    principal = Principal(
        id=profile.id,
        role="admin",
        active=True,
        verified=True,
        roles=frozenset({"admin"}),
        permissions=frozenset({"catalogs.manage", "audit.read"}),
    )
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client(
    db_session: AsyncSession, _professional_types_table: None
) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_professional_type_crud_flow(client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await client.post(f"{PREFIX}/professional-types", json={"name": "Doctor"})
    assert resp.status_code == 201, resp.text
    professional_type = resp.json()
    assert professional_type["name"] == "Doctor"
    assert professional_type["status"] == "active"
    assert professional_type["deleted_at"] is None
    professional_type_id = professional_type["id"]

    got = await client.get(f"{PREFIX}/professional-types/{professional_type_id}")
    assert got.status_code == 200
    assert got.json()["id"] == professional_type_id

    listed = await client.get(f"{PREFIX}/professional-types")
    assert listed.status_code == 200
    assert any(item["id"] == professional_type_id for item in listed.json())

    patched = await client.patch(
        f"{PREFIX}/professional-types/{professional_type_id}",
        json={"name": "Specialist", "status": "inactive"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Specialist"
    assert patched.json()["status"] == "inactive"

    deleted = await client.delete(f"{PREFIX}/professional-types/{professional_type_id}")
    assert deleted.status_code == 204
    assert (
        await client.get(f"{PREFIX}/professional-types/{professional_type_id}")
    ).status_code == 404

    listed_after_delete = await client.get(f"{PREFIX}/professional-types")
    assert all(item["id"] != professional_type_id for item in listed_after_delete.json())

    audit_resp = await client.get(f"{PREFIX}/audit-log", params={"resource": "professional_types"})
    entries = [e for e in audit_resp.json() if e["resource_id"] == professional_type_id]
    assert sorted(e["action"] for e in entries) == sorted(
        ["catalog.created", "catalog.updated", "catalog.deleted"]
    )
    assert (
        await client.patch(
            f"{PREFIX}/professional-types/{professional_type_id}", json={"name": "Other"}
        )
    ).status_code == 404
    assert (
        await client.delete(f"{PREFIX}/professional-types/{professional_type_id}")
    ).status_code == 404

    row = await db_session.get(ProfessionalType, uuid.UUID(professional_type_id))
    assert row is not None
    assert row.status == "deleted"
    assert row.deleted_at is not None


async def test_professional_type_not_found(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"{PREFIX}/professional-types/{missing}")).status_code == 404
    assert (
        await client.patch(f"{PREFIX}/professional-types/{missing}", json={"name": "Doctor"})
    ).status_code == 404
    assert (await client.delete(f"{PREFIX}/professional-types/{missing}")).status_code == 404


async def test_professional_type_validation_error(client: AsyncClient) -> None:
    assert (
        await client.post(f"{PREFIX}/professional-types", json={"name": "A"})
    ).status_code == 422
    assert (
        await client.patch(
            f"{PREFIX}/professional-types/{uuid.uuid4()}", json={"status": "deleted"}
        )
    ).status_code == 422
    assert (
        await client.patch(f"{PREFIX}/professional-types/{uuid.uuid4()}", json={"name": None})
    ).status_code == 422


async def test_professional_type_list_order_is_deterministic(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    created_at = datetime(2099, 1, 1, tzinfo=UTC)
    low_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    high_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    db_session.add_all(
        [
            ProfessionalType(id=low_id, name="A", created_at=created_at, updated_at=created_at),
            ProfessionalType(id=high_id, name="B", created_at=created_at, updated_at=created_at),
        ]
    )
    await db_session.commit()

    listed = await client.get(f"{PREFIX}/professional-types", params={"limit": 2})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [str(high_id), str(low_id)]


async def test_professional_type_rbac(
    auth_client: AsyncClient, db_session: AsyncSession, admin_identity: Profile
) -> None:
    patient = make_profile(role="patient")
    doctor = make_profile(role="doctor", specialty="Cardiología")
    db_session.add_all([patient, doctor])
    await db_session.flush()

    assert (await auth_client.get(f"{PREFIX}/professional-types", headers={})).status_code == 200
    assert (
        await auth_client.get(f"{PREFIX}/professional-types", headers=auth_headers(patient.id))
    ).status_code == 200
    assert (
        await auth_client.post(
            f"{PREFIX}/professional-types",
            json={"name": "Unauthorized"},
            headers=auth_headers(doctor.id),
        )
    ).status_code == 403
    assert (
        await auth_client.post(
            f"{PREFIX}/professional-types",
            json={"name": "Authorized"},
            headers=auth_headers(admin_identity.id),
        )
    ).status_code == 201


async def test_professional_type_soft_deleted_is_hidden(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row = ProfessionalType(name="Deleted", status="deleted")
    db_session.add(row)
    await db_session.commit()

    listed = await client.get(f"{PREFIX}/professional-types")
    assert listed.status_code == 200
    assert all(item["id"] != str(row.id) for item in listed.json())

    result = await db_session.execute(
        select(ProfessionalType).where(ProfessionalType.id == row.id)
    )
    assert result.scalar_one().status == "deleted"


async def test_professional_type_inactivo_oculto_en_publico_visible_en_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Desactivar un tipo lo saca del listado público (registro de médicos / pool)
    pero sigue visible en /admin para gestionarlo — mismo patrón que specialties."""
    row = ProfessionalType(name="Inactivo QA", status="inactive")
    db_session.add(row)
    await db_session.commit()

    publico = await client.get(f"{PREFIX}/professional-types", params={"limit": 100})
    assert all(item["id"] != str(row.id) for item in publico.json())

    admin = await client.get(f"{PREFIX}/professional-types/admin", params={"limit": 100})
    assert admin.status_code == 200
    assert any(item["id"] == str(row.id) for item in admin.json())


async def test_professional_type_admin_requiere_permiso(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    doctor = make_profile(role="doctor")  # doctor no tiene catalogs.manage
    db_session.add(doctor)
    await db_session.flush()
    resp = await auth_client.get(
        f"{PREFIX}/professional-types/admin", headers=auth_headers(doctor.id)
    )
    assert resp.status_code == 403
