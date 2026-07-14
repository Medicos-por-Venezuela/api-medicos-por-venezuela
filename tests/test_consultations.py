"""Pruebas del recurso consultations y sus eventos (CRUD aislado)."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


# --- Panel médico: claim atómico + cola (Realtime lo consume del backend) ---


async def _create_waiting_consultation(client: AsyncClient) -> str:
    """Crea una consulta en espera. Sin envejecerla: el panel ya no tiene gate de 20 min, así
    que una consulta recién creada debe aparecer en la cola de inmediato (tiempo real)."""
    patient_id = await _create_patient(client)
    return (
        await client.post(f"{PREFIX}/consultations", json={"patient_id": patient_id})
    ).json()["id"]


async def test_claim_es_atomico_solo_gana_un_medico(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Dos médicos toman el mismo caso: el primero 200, el segundo 409 (nunca ambos)."""
    cid = await _create_waiting_consultation(client)
    d1, d2 = make_profile(role="doctor"), make_profile(role="doctor")
    db_session.add_all([d1, d2])
    await db_session.flush()

    r1 = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(d1.id)
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["assigned_doctor_id"] == str(d1.id)
    assert r1.json()["status"] == "in_progress"

    r2 = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(d2.id)
    )
    assert r2.status_code == 409, r2.text


async def test_claim_via_whatsapp_marca_el_flag(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    cid = await _create_waiting_consultation(client)
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()

    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/claim",
        json={"via_whatsapp": True},
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["attended_via_whatsapp"] is True


async def test_claim_requiere_permiso_queue_take(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    cid = await _create_waiting_consultation(client)
    patient = make_profile(role="patient")
    db_session.add(patient)
    await db_session.flush()

    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(patient.id)
    )
    assert resp.status_code == 403, resp.text


async def test_panel_devuelve_espera_mias_y_cerradas(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()

    cid_waiting = await _create_waiting_consultation(client)
    cid_mine = await _create_waiting_consultation(client)
    await client.post(
        f"{PREFIX}/consultations/{cid_mine}/claim", json={}, headers=auth_headers(doc.id)
    )
    # Una consulta con especialidad explícita: el panel debe traer el NOMBRE resuelto
    # (es la columna con la que matchea el médico en el frontend).
    specs = (await client.get(f"{PREFIX}/specialties")).json()
    pediatria = next(s["id"] for s in specs if s["name"] == "Pediatría")
    patient_id = await _create_patient(client)
    cid_spec = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": patient_id, "specialty_id": pediatria},
        )
    ).json()["id"]

    resp = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    waiting_ids = {c["id"] for c in data["waiting"]}
    mine_ids = {c["id"] for c in data["mine"]}

    assert cid_waiting in waiting_ids
    assert cid_mine in mine_ids
    assert cid_mine not in waiting_ids  # ya asignada: sale de la cola de espera
    # el paciente viene anidado en cada fila (el card lo necesita)
    item = next(c for c in data["waiting"] if c["id"] == cid_waiting)
    assert item["patient"]["full_name"] == "Paciente Consulta"
    assert item["specialty"] is None  # sin specialty_id: el matching cae al legacy
    item_spec = next(c for c in data["waiting"] if c["id"] == cid_spec)
    assert item_spec["specialty"] == "Pediatría"
    assert isinstance(data["my_closed_count"], int)


async def test_panel_requiere_permiso_queue_read(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient = make_profile(role="patient")
    db_session.add(patient)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(patient.id))
    assert resp.status_code == 403, resp.text


# --- Anti-IDOR: pertenencia en update/close (security.md) ---


async def _consultation_assigned_to(
    client: AsyncClient, db_session: AsyncSession, doctor_id: str
) -> str:
    """Consulta asignada a `doctor_id` (asignada por el client admin del fixture)."""
    patient_id = await _create_patient(client)
    cid = (await client.post(f"{PREFIX}/consultations", json={"patient_id": patient_id})).json()[
        "id"
    ]
    assigned = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"status": "in_progress", "assigned_doctor_id": doctor_id},
    )
    assert assigned.status_code == 200, assigned.text
    return cid


async def test_doctor_no_puede_editar_ni_cerrar_consulta_ajena(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dr_a = make_profile(role="doctor")
    dr_b = make_profile(role="doctor")
    db_session.add_all([dr_a, dr_b])
    await db_session.flush()
    cid = await _consultation_assigned_to(client, db_session, str(dr_b.id))

    headers_a = auth_headers(dr_a.id)
    patched = await client.patch(
        f"{PREFIX}/consultations/{cid}", json={"internal_note": "intruso"}, headers=headers_a
    )
    assert patched.status_code == 409

    closed = await client.post(
        f"{PREFIX}/consultations/{cid}/close", json={"outcome": "closed"}, headers=headers_a
    )
    assert closed.status_code == 409


async def test_doctor_no_puede_reasignar_a_terceros(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dr_a = make_profile(role="doctor")
    dr_c = make_profile(role="doctor")
    db_session.add_all([dr_a, dr_c])
    await db_session.flush()
    patient_id = await _create_patient(client)
    cid = (await client.post(f"{PREFIX}/consultations", json={"patient_id": patient_id})).json()[
        "id"
    ]

    # Sin asignar: A no puede asignársela a C...
    resp = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(dr_c.id)},
        headers=auth_headers(dr_a.id),
    )
    assert resp.status_code == 409

    # ...ni tomarla para sí por PATCH: sería read-then-write y reabriría la carrera
    # que el claim atómico resuelve en la base (tomar = POST /{id}/claim).
    by_patch = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(dr_a.id)},
        headers=auth_headers(dr_a.id),
    )
    assert by_patch.status_code == 409

    # La toma vía claim; ya suya, puede editarla (incluido el no-op de assigned)...
    took = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(dr_a.id)
    )
    assert took.status_code == 200, took.text
    mine = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": str(dr_a.id), "internal_note": "mía"},
        headers=auth_headers(dr_a.id),
    )
    assert mine.status_code == 200, mine.text

    # ...liberarla (None) y cerrarla como propia tras re-tomarla.
    released = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"assigned_doctor_id": None},
        headers=auth_headers(dr_a.id),
    )
    assert released.status_code == 200, released.text
    await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(dr_a.id)
    )
    closed = await client.post(
        f"{PREFIX}/consultations/{cid}/close",
        json={"outcome": "closed"},
        headers=auth_headers(dr_a.id),
    )
    assert closed.status_code == 200, closed.text


async def test_doctor_no_puede_editar_doctor_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """doctor_id (ficha del médico) es server-only: un no-admin no lo edita por PATCH."""
    dr_a = make_profile(role="doctor")
    db_session.add(dr_a)
    await db_session.flush()
    patient_id = await _create_patient(client)
    cid = (await client.post(f"{PREFIX}/consultations", json={"patient_id": patient_id})).json()[
        "id"
    ]

    resp = await client.patch(
        f"{PREFIX}/consultations/{cid}",
        json={"doctor_id": "00000000-0000-0000-0000-000000000001"},
        headers=auth_headers(dr_a.id),
    )
    assert resp.status_code == 409


async def test_doctor_no_puede_inyectar_eventos_en_consulta_ajena(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Anti-IDOR en eventos: el historial del caso solo lo escribe el médico asignado
    (o un admin) — sin esto, cualquier doctor podía fabricar un evento 'closed' falso."""
    dr_a = make_profile(role="doctor")
    dr_b = make_profile(role="doctor")
    db_session.add_all([dr_a, dr_b])
    await db_session.flush()
    cid = await _consultation_assigned_to(client, db_session, str(dr_b.id))

    payload = {"consultation_id": cid, "event_type": "closed", "note": "evento intruso"}
    intruder = await client.post(
        f"{PREFIX}/consultations/{cid}/events", json=payload, headers=auth_headers(dr_a.id)
    )
    assert intruder.status_code == 409

    owner = await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "note", "note": "del asignado"},
        headers=auth_headers(dr_b.id),
    )
    assert owner.status_code == 201, owner.text

    admin = await client.post(  # el client del fixture es admin
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "note", "note": "del admin"},
    )
    assert admin.status_code == 201, admin.text


async def test_admin_puede_gestionar_consulta_ajena(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dr_b = make_profile(role="doctor")
    db_session.add(dr_b)
    await db_session.flush()
    cid = await _consultation_assigned_to(client, db_session, str(dr_b.id))

    # El client del fixture es admin: puede editar y cerrar consultas de otros.
    patched = await client.patch(f"{PREFIX}/consultations/{cid}", json={"internal_note": "admin"})
    assert patched.status_code == 200
    closed = await client.post(f"{PREFIX}/consultations/{cid}/close", json={"outcome": "closed"})
    assert closed.status_code == 200
