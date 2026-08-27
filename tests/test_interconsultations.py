"""Tests de Interconsultas: segunda opinión en tiempo real. Ver .knowledge/interconsultas.md.

Foco de seguridad: el médico INVITADO ve solo motivo, notas y edad — NUNCA la identidad del
paciente (nombre/cédula/teléfono/zona).
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests._helpers import add_doctor, any_specialty_id, auth_headers

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
        json={
            "patient_id": p.json()["id"],
            "chief_complaint": "Dolor de pecho",
            "specialty_id": await any_specialty_id(client),
        },
    )
    assert c.status_code == 201, c.text
    return c.json()["id"]


async def _claim(client: AsyncClient, cid: str, doctor_id) -> None:
    r = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(doctor_id)
    )
    assert r.status_code == 200, r.text


async def test_la_interconsulta_se_persiste_de_verdad(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Regresión del bug de producción: la API respondía 201 con un id real y la fila NO existía.

    `create_interconsultation` hacía `flush()` sin `commit()`. El flush manda el INSERT y rellena
    `inter.id` — por eso la respuesta se veía perfecta — pero `get_db` cierra la sesión al acabar
    el request y eso hace ROLLBACK. En prod: 201 correctos y `count(*) = 0`.

    No se puede comprobar por visibilidad de datos: `db_session` usa
    `join_transaction_mode="create_savepoint"`, y dentro de la misma sesión `flush()` y `commit()`
    son indistinguibles — por eso los 269 tests pasaban con el bug dentro. Así que se comprueba lo
    único que los distingue: que la llamada COMMITEA. Si alguien vuelve a quitar el commit, esto
    se pone rojo aunque la respuesta siga siendo un 201 impecable.
    """
    attending = await add_doctor(db_session)
    invited = await add_doctor(db_session)
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    commits = 0
    original_commit = db_session.commit

    async def contar_commits() -> None:
        nonlocal commits
        commits += 1
        await original_commit()

    db_session.commit = contar_commits  # type: ignore[method-assign]
    try:
        resp = await client.post(
            f"{PREFIX}/interconsultations",
            json={"consultation_id": cid, "invited_doctor_id": str(invited.id)},
            headers=auth_headers(attending.id),
        )
    finally:
        db_session.commit = original_commit  # type: ignore[method-assign]

    assert resp.status_code == 201, resp.text
    assert commits == 1, (
        "create_interconsultation no commiteó: get_db hace rollback al cerrar la sesión, así que "
        "la fila se descarta aunque la API responda 201 con un id real (bug de prod 2026-08-02)"
    )


async def test_create_and_invitee_limited_view(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    attending = await add_doctor(db_session)
    invited = await add_doctor(db_session)

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
    attending = await add_doctor(db_session)
    invited = await add_doctor(db_session)
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
    attending = await add_doctor(db_session)
    other = await add_doctor(db_session)
    invited = await add_doctor(db_session)
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    resp = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(invited.id)},
        headers=auth_headers(other.id),  # no es el que atiende
    )
    assert resp.status_code == 403


async def test_cannot_invite_self(client: AsyncClient, db_session: AsyncSession) -> None:
    attending = await add_doctor(db_session)
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    resp = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(attending.id)},
        headers=auth_headers(attending.id),
    )
    assert resp.status_code == 409
