"""Tests for specialty matching and CRUD."""

import uuid
from collections.abc import AsyncGenerator

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_db
from src.main import app
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.services.queue_access import ESPECIALIDAD_POR_DEFINIR, SIN_ESPECIALIDAD, queue_scope
from src.services.specialties import compute_priority
from tests._helpers import add_doctor, auth_headers, make_profile

PREFIX = "/api/v1"


# --- reglas del catálogo que decide la cola (ver también tests/test_cola_especialidad.py) ---


async def test_renombrar_psicologia_no_abre_la_cola_a_su_admin(db_session: AsyncSession) -> None:
    """La restricción del admin de salud mental cuelga de la FILA (`mental_health_only`), así
    que renombrar la especialidad no la afecta. Antes las reglas eran literales de nombres y un
    renombre del catálogo las abría en silencio."""
    psico = (
        await db_session.execute(
            select(Specialty).where(
                func.lower(Specialty.name) == "psicología", Specialty.deleted_at.is_(None)
            )
        )
    ).scalar_one()
    psico.name = "Psicología clínica y de la salud"
    await db_session.flush()

    scope = await queue_scope(db_session, specialty_id=psico.id, is_admin=True)
    assert scope.specialty_ids == frozenset({psico.id})


async def test_renombrar_otra_no_la_vuelve_una_cola(db_session: AsyncSession) -> None:
    otra = (
        await db_session.execute(select(Specialty).where(func.lower(Specialty.name) == "otra"))
    ).scalar_one()
    otra.name = "Otra especialidad"
    await db_session.flush()

    scope = await queue_scope(db_session, specialty_id=otra.id, is_admin=False)
    assert scope.specialty_ids == frozenset()
    assert scope.blocked_reason == ESPECIALIDAD_POR_DEFINIR


async def test_especialidad_inexistente_es_fail_closed(db_session: AsyncSession) -> None:
    """Sin FK, o con un id que ya no está en el catálogo, no se ve ninguna cola."""
    for specialty_id in (None, uuid.UUID("00000000-0000-0000-0000-000000000000")):
        scope = await queue_scope(db_session, specialty_id=specialty_id, is_admin=False)
        assert scope.specialty_ids == frozenset()
        assert scope.blocked_reason == SIN_ESPECIALIDAD
        assert scope.allows(None) is False


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

    audit_resp = await client.get(f"{PREFIX}/audit-log", params={"resource": "specialties"})
    entries = [e for e in audit_resp.json() if e["resource_id"] == specialty_id]
    assert sorted(e["action"] for e in entries) == sorted(
        ["catalog.created", "catalog.updated", "catalog.deleted"]
    )


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


# --- selector de interconsulta (flag available_for_interconsultation) ---


async def test_medicina_general_no_es_pedible_en_interconsulta(db_session: AsyncSession) -> None:
    """La regla vive en la COLUMNA, no en un literal: el seed de la migración apagó el flag de
    Medicina general. Si alguien renombra esa fila del catálogo, la regla sigue en pie."""
    flag = await db_session.scalar(
        select(Specialty.available_for_interconsultation).where(
            func.lower(Specialty.name) == "medicina general",
            Specialty.deleted_at.is_(None),
        )
    )
    assert flag is False, "Medicina general debe quedar fuera del selector de interconsultas"


async def test_listado_publico_filtra_por_flag_de_interconsulta(client: AsyncClient) -> None:
    """`?for_interconsultation=true` es el selector del médico tratante: solo especialidades
    de verdad. Sin el filtro, el catálogo sigue completo (lo usa el registro de médicos)."""
    solo_pedibles = await client.get(f"{PREFIX}/specialties?for_interconsultation=true&limit=100")
    assert solo_pedibles.status_code == 200, solo_pedibles.text
    nombres = {s["name"].lower() for s in solo_pedibles.json()}
    assert "medicina general" not in nombres
    assert all(s["available_for_interconsultation"] for s in solo_pedibles.json())

    completo = await client.get(f"{PREFIX}/specialties?limit=100")
    assert completo.status_code == 200
    assert len(completo.json()) >= len(solo_pedibles.json())


async def test_admin_puede_reincorporar_una_especialidad_al_selector(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Excluir o reincorporar una especialidad es un PATCH, no un despliegue."""
    especialidad = Specialty(name=f"Nefrología {uuid.uuid4().hex[:8]}")
    db_session.add(especialidad)
    await db_session.flush()

    # Nace pedible (default true: el fallo abierto es el correcto para un catálogo).
    assert especialidad.available_for_interconsultation is True

    apagada = await client.patch(
        f"{PREFIX}/specialties/{especialidad.id}",
        json={"available_for_interconsultation": False},
    )
    assert apagada.status_code == 200, apagada.text
    assert apagada.json()["available_for_interconsultation"] is False


async def test_catalogo_con_with_doctors_solo_colas_con_medico(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`?with_doctors=true` (selector del registro de pacientes) aplica la MISMA condición que
    la derivación: sin un médico habilitado mirando la cola, la especialidad no se ofrece."""
    nueva = Specialty(name=f"Especialidad Sin Medico {uuid.uuid4().hex[:6]}", status="active")
    db_session.add(nueva)
    await db_session.flush()

    con_medicos = (await client.get(f"{PREFIX}/specialties?with_doctors=true&limit=100")).json()
    assert str(nueva.id) not in {s["id"] for s in con_medicos}

    # Sin el filtro sigue apareciendo: el catálogo del resto del panel no cambia.
    todas = (await client.get(f"{PREFIX}/specialties?limit=100")).json()
    assert str(nueva.id) in {s["id"] for s in todas}

    # Con un médico habilitado (ficha verificada, cédula y licencia) sí se ofrece.
    await add_doctor(db_session, specialty=nueva.name)
    con_medicos = (await client.get(f"{PREFIX}/specialties?with_doctors=true&limit=100")).json()
    assert str(nueva.id) in {s["id"] for s in con_medicos}
