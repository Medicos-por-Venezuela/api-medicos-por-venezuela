"""Pruebas del recurso consultations y sus eventos (CRUD aislado)."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.specialty import Specialty

PREFIX = "/api/v1"


async def _create_patient(client: AsyncClient) -> str:
    resp = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Paciente Consulta",
            "phone_whatsapp": "+58412555000",
            "affected_zone": "Caracas",
            "consent": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_consultation_crud_and_code_autogeneration(client: AsyncClient) -> None:
    patient_id = await _create_patient(client)

    # code omitido -> lo genera el trigger generate_consultation_code.
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={"patient_id": patient_id, "chief_complaint": "Dolor de cabeza"},
    )
    assert resp.status_code == 201, resp.text
    consultation = resp.json()
    assert consultation["code"]  # autogenerado
    assert consultation["status"] == "waiting"
    cid = consultation["id"]

    # Get
    assert (await client.get(f"{PREFIX}/consultations/{cid}")).status_code == 200

    # List con filtros
    listed = await client.get(
        f"{PREFIX}/consultations", params={"status": "waiting", "patient_id": patient_id}
    )
    assert listed.status_code == 200
    assert any(c["id"] == cid for c in listed.json())

    # Patch estado
    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}", json={"status": "closed", "internal_note": "ok"}
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "closed"

    # Delete
    assert (await client.delete(f"{PREFIX}/consultations/{cid}")).status_code == 204
    assert (await client.get(f"{PREFIX}/consultations/{cid}")).status_code == 404

    audit_resp = await client.get(f"{PREFIX}/audit-log", params={"resource": "consultations"})
    entries = [e for e in audit_resp.json() if e["resource_id"] == cid]
    assert sorted(e["action"] for e in entries) == sorted(
        ["consultation.updated", "consultation.deleted"]
    )


async def test_consultation_invalid_patient(client: AsyncClient) -> None:
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={"patient_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 400


async def test_consultation_invalid_status(client: AsyncClient) -> None:
    patient_id = await _create_patient(client)
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={"patient_id": patient_id, "status": "no_existe"},
    )
    assert resp.status_code == 422


async def test_consultation_code_is_server_generated(client: AsyncClient) -> None:
    patient_id = await _create_patient(client)

    # `code` no es un campo aceptado (extra="forbid"): se rechaza con 422.
    bad = await client.post(
        f"{PREFIX}/consultations", json={"patient_id": patient_id, "code": "NO-ACEPTADO"}
    )
    assert bad.status_code == 422

    # Sin enviar `code`, el trigger de la base genera el código automáticamente.
    resp = await client.post(f"{PREFIX}/consultations", json={"patient_id": str(patient_id)})
    assert resp.status_code == 201
    assert resp.json()["code"].startswith("CONS-")


async def test_consultation_not_found(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"{PREFIX}/consultations/{missing}")).status_code == 404
    assert (
        await client.patch(f"{PREFIX}/consultations/{missing}", json={"status": "closed"})
    ).status_code == 404
    assert (await client.delete(f"{PREFIX}/consultations/{missing}")).status_code == 404
    assert (await client.get(f"{PREFIX}/consultations/{missing}/events")).status_code == 404


async def test_consultation_events(client: AsyncClient) -> None:
    patient_id = await _create_patient(client)
    cid = (await client.post(f"{PREFIX}/consultations", json={"patient_id": patient_id})).json()[
        "id"
    ]

    # Crear evento (consultation_id coincide)
    ok = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "status_change", "note": "abierta"},
    )
    assert ok.status_code == 201, ok.text

    # Mismatch de consultation_id -> 400
    other = str(uuid.uuid4())
    bad = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": other, "event_type": "status_change"},
    )
    assert bad.status_code == 400

    # Listar
    listed = await client.get(f"{PREFIX}/consultations/{cid}/events")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


# --- specialty_id (reemplaza needs_tags para el matching del panel, aparte) ---


async def test_create_consultation_con_specialty_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient_id = await _create_patient(client)
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()

    resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "chief_complaint": "Control de rutina",
            "specialty_id": str(specialty.id),
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["specialty_id"] == str(specialty.id)


async def test_create_consultation_specialty_id_inexistente_falla_400(
    client: AsyncClient,
) -> None:
    patient_id = await _create_patient(client)
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "specialty_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert resp.status_code == 400
