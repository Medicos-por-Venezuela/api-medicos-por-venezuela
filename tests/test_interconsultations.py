"""Tests de Interconsultas: segunda opinión en tiempo real. Ver .knowledge/interconsultas.md.

Foco de seguridad: el médico INVITADO ve solo motivo, notas y edad — NUNCA la identidad del
paciente (nombre/cédula/teléfono/zona).
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


async def _consultation_with_patient(client: AsyncClient, *, age_range: str = "30-39") -> str:
    """Crea un paciente (con edad + nombre) y una consulta con motivo. Devuelve el cid."""
    p = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Paciente Secreto",
            "phone_whatsapp": "+58412555111",
            "affected_zone": "Caracas",
            "age_range": age_range,
            "consent": True,
        },
    )
    assert p.status_code == 201, p.text
    c = await client.post(
        f"{PREFIX}/consultations",
        json={"patient_id": p.json()["id"], "chief_complaint": "Dolor de pecho"},
    )
    assert c.status_code == 201, c.text
    return c.json()["id"]


async def _claim(client: AsyncClient, cid: str, doctor_id) -> None:
    r = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(doctor_id)
    )
    assert r.status_code == 200, r.text


async def test_create_and_invitee_limited_view(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    attending = make_profile(role="doctor")
    invited = make_profile(role="doctor")
    db_session.add_all([attending, invited])
    await db_session.flush()

    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    resp = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(invited.id), "note": "revisa"},
        headers=auth_headers(attending.id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["invited_doctor_id"] == str(invited.id)
    assert body["status"] == "active"
    assert body["invited_doctor_name"] == invited.full_name  # nombre del colega, no del paciente

    # El INVITADO ve datos LIMITADOS.
    me = await client.get(f"{PREFIX}/interconsultations/me", headers=auth_headers(invited.id))
    assert me.status_code == 200
    items = me.json()
    assert len(items) == 1
    item = items[0]
    assert item["consultation_id"] == cid
    assert item["chief_complaint"] == "Dolor de pecho"  # motivo
    assert item["patient_age_range"] == "30-39"  # edad (único dato del paciente)
    assert "video_room_url" in item
    # SIN identidad del paciente.
    assert "Paciente Secreto" not in me.text
    for leaked in ("patient_name", "full_name", "cedula", "phone_whatsapp", "affected_zone"):
        assert leaked not in item


async def test_one_interconsultation_per_consultation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    attending = make_profile(role="doctor")
    invited = make_profile(role="doctor")
    db_session.add_all([attending, invited])
    await db_session.flush()
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    ok = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(invited.id)},
        headers=auth_headers(attending.id),
    )
    assert ok.status_code == 201
    dup = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(invited.id)},
        headers=auth_headers(attending.id),
    )
    assert dup.status_code == 409


async def test_only_attending_doctor_can_create(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    attending = make_profile(role="doctor")
    other = make_profile(role="doctor")
    invited = make_profile(role="doctor")
    db_session.add_all([attending, other, invited])
    await db_session.flush()
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    resp = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(invited.id)},
        headers=auth_headers(other.id),  # no es el que atiende
    )
    assert resp.status_code == 403


async def test_cannot_invite_self(client: AsyncClient, db_session: AsyncSession) -> None:
    attending = make_profile(role="doctor")
    db_session.add(attending)
    await db_session.flush()
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    resp = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(attending.id)},
        headers=auth_headers(attending.id),
    )
    assert resp.status_code == 409
