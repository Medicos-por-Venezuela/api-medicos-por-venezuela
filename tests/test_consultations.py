"""Pruebas del recurso consultations y sus eventos (CRUD aislado)."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.specialty import Specialty
from tests._helpers import auth_headers, make_profile

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


# --- entered_call_at (paridad con el dashboard legacy: "en espera" = waiting +
#     el médico ya entró a la sala) ---------------------------------------------


async def test_consultation_entered_call_at_round_trips(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient_id = await _create_patient(client)
    cid = (await client.post(f"{PREFIX}/consultations", json={"patient_id": patient_id})).json()[
        "id"
    ]

    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    assert consultation.entered_call_at is None  # default: nadie ha entrado aún

    now = datetime.now(UTC)
    consultation.entered_call_at = now
    await db_session.flush()
    await db_session.refresh(consultation)
    assert consultation.entered_call_at is not None


# --- Enriquecimiento del listado (patient_name / assigned_doctor_name) --------


async def test_consultation_list_includes_patient_and_doctor_names(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient_id = await _create_patient(client)
    doctor_profile = make_profile(role="doctor")
    db_session.add(doctor_profile)
    await db_session.flush()

    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "chief_complaint": "Fiebre"},
        )
    ).json()["id"]

    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(doctor_profile.id), "status": "in_progress"},
    )
    assert patched.status_code == 200, patched.text

    listed = await client.get(f"{PREFIX}/consultations", params={"patient_id": patient_id})
    assert listed.status_code == 200
    row = next(c for c in listed.json() if c["id"] == cid)
    assert row["patient_name"] == "Paciente Consulta"
    assert row["assigned_doctor_name"] == doctor_profile.full_name


async def test_consultation_list_names_are_null_when_unassigned(client: AsyncClient) -> None:
    patient_id = await _create_patient(client)
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "chief_complaint": "Tos"},
        )
    ).json()["id"]

    listed = await client.get(f"{PREFIX}/consultations", params={"patient_id": patient_id})
    row = next(c for c in listed.json() if c["id"] == cid)
    assert row["patient_name"] == "Paciente Consulta"
    assert row["assigned_doctor_name"] is None


# --- Anti-PII: un paciente autenticado no debe recibir patient_name / -------
#     assigned_doctor_name (esos campos son enriquecimiento solo para staff) ---


async def test_consultation_list_hides_pii_from_patient_viewer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Guarda contra un futuro drift de `ConsultationPatientResponse`: si algún día
    se le agregan `patient_name`/`assigned_doctor_name`, este test debe fallar."""
    patient_id = await _create_patient(client)
    doctor_profile = make_profile(role="doctor")
    db_session.add(doctor_profile)
    await db_session.flush()

    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "chief_complaint": "Fiebre"},
        )
    ).json()["id"]
    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(doctor_profile.id), "status": "in_progress"},
    )
    assert patched.status_code == 200, patched.text

    # La cuenta (users) del paciente, ligada a su ficha (patients.user_id), es el
    # "viewer" no-staff que la API usa para filtrar por pertenencia (anti-IDOR).
    patient_profile = make_profile(role="patient")
    db_session.add(patient_profile)
    await db_session.flush()
    patient_row = await db_session.get(Patient, uuid.UUID(patient_id))
    patient_row.user_id = patient_profile.id
    await db_session.flush()

    listed = await client.get(
        f"{PREFIX}/consultations",
        params={"patient_id": patient_id},
        headers=auth_headers(patient_profile.id),
    )
    assert listed.status_code == 200, listed.text
    row = next(c for c in listed.json() if c["id"] == cid)
    assert "patient_name" not in row
    assert "assigned_doctor_name" not in row


# --- Regresión: ConsultationResponse no debe re-validar longitud de datos ------
#     ya persistidos (bug de producción: filas reales con chief_complaint > 500
#     causaban un 500 al listar/serializar). ---------------------------------


async def test_list_consultations_serializes_chief_complaint_longer_than_500(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient_id = await _create_patient(client)
    long_complaint = "a" * 600  # excede el max_length=500 que tenían los esquemas de entrada.

    consultation = Consultation(
        patient_id=uuid.UUID(patient_id),
        status="in_progress",
        chief_complaint=long_complaint,
    )
    db_session.add(consultation)
    await db_session.flush()

    listed = await client.get(
        f"{PREFIX}/consultations", params={"status": "in_progress", "limit": 100}
    )
    assert listed.status_code == 200, listed.text
    row = next(c for c in listed.json() if c["id"] == str(consultation.id))
    assert row["chief_complaint"] == long_complaint
    assert len(row["chief_complaint"]) == 600


async def test_list_consultations_filters_by_contacted_whatsapp_status(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient_id = await _create_patient(client)

    consultation = Consultation(
        patient_id=uuid.UUID(patient_id),
        status="contacted_whatsapp",
    )
    db_session.add(consultation)
    await db_session.flush()

    listed = await client.get(f"{PREFIX}/consultations", params={"status": "contacted_whatsapp"})
    assert listed.status_code == 200, listed.text
    assert any(c["id"] == str(consultation.id) for c in listed.json())
