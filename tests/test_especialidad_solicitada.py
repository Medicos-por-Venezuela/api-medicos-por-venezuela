"""Médicos con especialidad "Otra" (R6 de tasks/cola-por-especialidad/spec.md).

No ven la cola hasta que eligen una especialidad real o escriben la suya; la escrita queda
pendiente y un admin la resuelve asignándoles una del catálogo.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit_log import AuditLog
from src.models.doctor import Doctor
from src.models.profile import Profile
from src.models.specialty import Specialty
from tests._helpers import GENERAL, add_doctor, auth_headers, specialty_id_by_name

PREFIX = "/api/v1"


async def _doctor_row(db_session: AsyncSession, user_id) -> Doctor:
    doctor = await db_session.scalar(select(Doctor).where(Doctor.user_id == user_id))
    await db_session.refresh(doctor)
    return doctor


async def test_un_medico_puede_ejercer_varias_especialidades(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un internista que además es cardiólogo: guarda las dos y ve las dos colas. La primera queda
    como principal (la que usan el pool, los reportes y el admin)."""
    doc = await add_doctor(db_session, specialty=GENERAL)
    interna = await specialty_id_by_name(db_session, "Medicina interna")
    cardio = await specialty_id_by_name(db_session, "Cardiología")

    resp = await client.patch(
        f"{PREFIX}/doctors/me",
        json={"specialty_ids": [str(interna), str(cardio)]},
        headers=auth_headers(doc.id),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # La lista va en el orden del catálogo; la PRINCIPAL es la primera que envió.
    assert {s["name"] for s in body["specialties"]} == {"Medicina interna", "Cardiología"}
    assert body["specialty"] == "Medicina interna"
    panel = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    # La cola de entrada va primera (es la que más acumula), luego las suyas.
    assert [q["name"] for q in panel.json()["queues"]] == [
        GENERAL,
        "Medicina interna",
        "Cardiología",
    ]


async def test_elegir_varias_y_ademas_escribir_una(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Puede elegir las que sí están y, además, pedir la que falta: sigue viendo sus colas
    mientras un admin revisa la nueva."""
    doc = await add_doctor(db_session, specialty=GENERAL)
    cardio = await specialty_id_by_name(db_session, "Cardiología")

    resp = await client.patch(
        f"{PREFIX}/doctors/me",
        json={"specialty_ids": [str(cardio)], "requested_specialty": "Medicina del deporte"},
        headers=auth_headers(doc.id),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["name"] for s in body["specialties"]] == ["Cardiología"]
    assert body["requested_specialty"] == "Medicina del deporte"
    assert body["specialty_is_placeholder"] is False
    panel = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    assert panel.json()["queue_blocked_reason"] is None


async def test_el_admin_suma_la_especialidad_pedida_a_las_que_ya_tenia(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    cardio = await specialty_id_by_name(db_session, "Cardiología")
    await client.patch(
        f"{PREFIX}/doctors/me",
        json={"specialty_ids": [str(cardio)], "requested_specialty": "Medicina del deporte"},
        headers=auth_headers(doc.id),
    )
    row = await _doctor_row(db_session, doc.id)
    nueva = Specialty(name=f"Medicina del deporte {uuid.uuid4().hex[:6]}")
    db_session.add(nueva)
    await db_session.flush()

    resp = await client.post(
        f"{PREFIX}/doctors/{row.id}/specialty-request/resolve",
        json={"specialty_id": str(nueva.id)},
    )

    assert resp.status_code == 200, resp.text
    me = (await client.get(f"{PREFIX}/doctors/me", headers=auth_headers(doc.id))).json()
    assert {s["name"] for s in me["specialties"]} == {"Cardiología", nueva.name}
    assert me["specialty"] == "Cardiología"  # la principal no cambia si ya tenía una real


async def test_el_perfil_dice_si_la_especialidad_es_de_relleno(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    otra = await add_doctor(db_session, specialty="Otra")
    general = await add_doctor(db_session, specialty=GENERAL)

    me_otra = (await client.get(f"{PREFIX}/doctors/me", headers=auth_headers(otra.id))).json()
    me_general = (
        await client.get(f"{PREFIX}/doctors/me", headers=auth_headers(general.id))
    ).json()
    assert me_otra["specialty_is_placeholder"] is True
    assert me_general["specialty_is_placeholder"] is False
    assert me_otra["requested_specialty"] is None


async def test_escribir_una_especialidad_la_deja_pendiente_y_sin_cola(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)

    resp = await client.patch(
        f"{PREFIX}/doctors/me",
        json={"requested_specialty": "  Medicina   del deporte "},
        headers=auth_headers(doc.id),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested_specialty"] == "Medicina del deporte"
    assert body["specialty_is_placeholder"] is True
    row = await _doctor_row(db_session, doc.id)
    assert row.requested_specialty_at is not None
    # La cuenta queda con la especialidad de relleno: no le llegan casos de la anterior.
    await db_session.refresh(await db_session.get(Profile, doc.id))
    panel = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    assert panel.json()["queue_blocked_reason"] == "especialidad_por_definir"


async def test_elegir_una_real_descarta_la_solicitud(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty="Otra")
    await client.patch(
        f"{PREFIX}/doctors/me",
        json={"requested_specialty": "Medicina del deporte"},
        headers=auth_headers(doc.id),
    )

    resp = await client.patch(
        f"{PREFIX}/doctors/me",
        json={"specialty_id": str(await specialty_id_by_name(db_session, GENERAL))},
        headers=auth_headers(doc.id),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["requested_specialty"] is None
    assert resp.json()["specialty"] == GENERAL
    panel = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    assert panel.json()["queue_blocked_reason"] is None


async def test_la_especialidad_escrita_se_valida(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty="Otra")
    for valor in ("x", "<b>Cardio</b>", "a" * 121):
        resp = await client.patch(
            f"{PREFIX}/doctors/me",
            json={"requested_specialty": valor},
            headers=auth_headers(doc.id),
        )
        assert resp.status_code == 422, (valor, resp.text)


async def test_el_admin_ve_las_pendientes_y_las_resuelve(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty="Otra")
    marca = uuid.uuid4().hex[:6]
    await client.patch(
        f"{PREFIX}/doctors/me",
        json={"requested_specialty": f"Medicina del deporte {marca}"},
        headers=auth_headers(doc.id),
    )
    row = await _doctor_row(db_session, doc.id)

    listado = await client.get(f"{PREFIX}/doctors/specialty-requests")
    assert listado.status_code == 200, listado.text
    item = next(i for i in listado.json()["items"] if i["doctor_id"] == str(row.id))
    assert item["requested_specialty"] == f"Medicina del deporte {marca}"
    assert item["specialty"] == "Otra"
    assert listado.json()["total"] >= 1

    # El admin la agrega al catálogo y se la asigna.
    nueva = Specialty(name=f"Medicina del deporte {marca}")
    db_session.add(nueva)
    await db_session.flush()
    resp = await client.post(
        f"{PREFIX}/doctors/{row.id}/specialty-request/resolve",
        json={"specialty_id": str(nueva.id)},
    )
    assert resp.status_code == 200, resp.text

    row = await _doctor_row(db_session, doc.id)
    assert row.specialty_id == nueva.id
    assert row.requested_specialty is None
    cuenta = await db_session.get(Profile, doc.id)
    await db_session.refresh(cuenta)
    assert cuenta.specialty_id == nueva.id  # la cola sale de la cuenta: tiene que sincronizarse
    audit = (
        await db_session.scalars(
            select(AuditLog).where(
                AuditLog.action == "doctor.specialty_request_resolved",
                AuditLog.resource_id == str(row.id),
            )
        )
    ).all()
    assert len(audit) == 1

    otra_vez = await client.post(
        f"{PREFIX}/doctors/{row.id}/specialty-request/resolve",
        json={"specialty_id": str(nueva.id)},
    )
    assert otra_vez.status_code == 409


async def test_no_se_resuelve_con_otra(client: AsyncClient, db_session: AsyncSession) -> None:
    doc = await add_doctor(db_session, specialty="Otra")
    await client.patch(
        f"{PREFIX}/doctors/me",
        json={"requested_specialty": "Medicina del deporte"},
        headers=auth_headers(doc.id),
    )
    row = await _doctor_row(db_session, doc.id)

    resp = await client.post(
        f"{PREFIX}/doctors/{row.id}/specialty-request/resolve",
        json={"specialty_id": str(await specialty_id_by_name(db_session, "Otra"))},
    )
    assert resp.status_code == 422, resp.text


async def test_un_medico_no_ve_la_bandeja_del_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    resp = await client.get(f"{PREFIX}/doctors/specialty-requests", headers=auth_headers(doc.id))
    assert resp.status_code == 403
