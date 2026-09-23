"""Tests de Interconsultas: segunda opinión en tiempo real. Ver .knowledge/interconsultas.md.

Foco de seguridad: el médico INVITADO ve solo motivo, notas y edad — NUNCA la identidad del
paciente (nombre/cédula/teléfono/zona).
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit_log import AuditLog
from src.services.clinical_access import READ_CLINICAL_DATA
from tests._helpers import GENERAL, add_doctor, any_specialty_id, auth_headers

PREFIX = "/api/v1"


async def _consultation_with_patient(client: AsyncClient, *, age_range: str = "30-39") -> str:
    """Crea un paciente (con edad + nombre) y una consulta con motivo. Devuelve el cid."""
    p = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Paciente Secreto",
            "phone_whatsapp": "+58412555111",
            "emergency_phone": "+58414555111",
            "address_encrypted": "v1:dGVzdCBjaXBoZXJ0ZXh0",
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
    attending = await add_doctor(db_session, specialty=GENERAL)
    invited = await add_doctor(db_session, specialty=GENERAL)
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
    # Dos commits: el del alta y el de la traza `READ_CLINICAL_DATA` (la respuesta devuelve la
    # nota en claro al que atiende, y esa lectura se audita con su propio commit). Si alguien
    # quita el del alta, queda 1 y esto se pone rojo.
    assert commits == 2, (
        "create_interconsultation no commiteó: get_db hace rollback al cerrar la sesión, así que "
        "la fila se descarta aunque la API responda 201 con un id real (bug de prod 2026-08-02)"
    )


async def test_create_and_invitee_limited_view(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    attending = await add_doctor(db_session, specialty=GENERAL)
    invited = await add_doctor(db_session, specialty=GENERAL)

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
    attending = await add_doctor(db_session, specialty=GENERAL)
    invited = await add_doctor(db_session, specialty=GENERAL)
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
    attending = await add_doctor(db_session, specialty=GENERAL)
    other = await add_doctor(db_session, specialty=GENERAL)
    invited = await add_doctor(db_session, specialty=GENERAL)
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    resp = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(invited.id)},
        headers=auth_headers(other.id),  # no es el que atiende
    )
    assert resp.status_code == 403


async def test_cannot_invite_self(client: AsyncClient, db_session: AsyncSession) -> None:
    attending = await add_doctor(db_session, specialty=GENERAL)
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    resp = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(attending.id)},
        headers=auth_headers(attending.id),
    )
    assert resp.status_code == 409


# --- Contenido clínico: equipo tratante del caso, admin redactado, resto 403 -----------------


async def _lecturas(db: AsyncSession, actor_id, outcome: str = "granted") -> list[AuditLog]:
    filas = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == READ_CLINICAL_DATA,
                AuditLog.actor_user_id == actor_id,
                AuditLog.resource == "consultations",
            )
        )
    ).scalars()
    return [f for f in filas if f.metadata_["outcome"] == outcome]


async def _con_interconsulta(client: AsyncClient, db_session: AsyncSession):
    attending = await add_doctor(db_session, specialty=GENERAL)
    invited = await add_doctor(db_session, specialty=GENERAL)
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)
    resp = await client.post(
        f"{PREFIX}/interconsultations",
        json={"consultation_id": cid, "invited_doctor_id": str(invited.id), "note": "revisa ECG"},
        headers=auth_headers(attending.id),
    )
    assert resp.status_code == 201, resp.text
    return attending, invited, cid, resp.json()


async def test_el_que_atiende_recibe_su_nota_y_el_invitado_el_caso_auditado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    attending, invited, cid, creada = await _con_interconsulta(client, db_session)
    assert creada["note"] == "revisa ECG"
    assert creada["clinical_access"] == "full"

    me = await client.get(f"{PREFIX}/interconsultations/me", headers=auth_headers(invited.id))
    assert me.status_code == 200, me.text
    (item,) = me.json()
    assert item["chief_complaint"] == "Dolor de pecho"
    assert item["note"] == "revisa ECG"
    assert item["clinical_access"] == "full"

    (entrada,) = await _lecturas(db_session, invited.id)
    assert entrada.metadata_["via"] == "interconsultation"
    assert entrada.metadata_["ids"] == [cid]
    assert entrada.resource_id == cid
    assert entrada.metadata_["tiers"] == ["notes", "summary"]


async def test_for_consultation_tratante_ve_la_nota_admin_redactado(
    client: AsyncClient, db_session: AsyncSession, admin_identity
) -> None:
    attending, _, cid, _ = await _con_interconsulta(client, db_session)

    suya = await client.get(
        f"{PREFIX}/interconsultations/for-consultation/{cid}", headers=auth_headers(attending.id)
    )
    assert suya.status_code == 200, suya.text
    assert suya.json()["note"] == "revisa ECG"
    assert suya.json()["clinical_access"] == "full"

    # El admin opera (ve que existe, a quién se invitó y el estado) sin leer la nota.
    admin = await client.get(f"{PREFIX}/interconsultations/for-consultation/{cid}")
    assert admin.status_code == 200, admin.text
    assert admin.json()["note"] is None
    assert admin.json()["clinical_access"] == "none"
    assert admin.json()["status"] == "active"
    assert await _lecturas(db_session, admin_identity.id) == []


async def test_for_consultation_otro_medico_403_y_queda_auditado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cierra la brecha: antes bastaba ser staff para leer la interconsulta de cualquier caso.
    Tampoco el INVITADO entra por acá: su vista (datos limitados) es `/interconsultations/me`."""
    _, invited, cid, _ = await _con_interconsulta(client, db_session)
    otro = await add_doctor(db_session, specialty=GENERAL)

    for intruso in (otro, invited):
        resp = await client.get(
            f"{PREFIX}/interconsultations/for-consultation/{cid}",
            headers=auth_headers(intruso.id),
        )
        assert resp.status_code == 403, resp.text
        assert "revisa" not in resp.text
        (denegada,) = await _lecturas(db_session, intruso.id, outcome="denied")
        assert denegada.resource_id == cid


async def test_for_consultation_sin_interconsulta_y_consulta_inexistente(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    attending = await add_doctor(db_session, specialty=GENERAL)
    cid = await _consultation_with_patient(client)
    await _claim(client, cid, attending.id)

    vacia = await client.get(
        f"{PREFIX}/interconsultations/for-consultation/{cid}", headers=auth_headers(attending.id)
    )
    assert vacia.status_code == 200
    assert vacia.json() is None

    inexistente = await client.get(f"{PREFIX}/interconsultations/for-consultation/{uuid.uuid4()}")
    assert inexistente.status_code == 404
