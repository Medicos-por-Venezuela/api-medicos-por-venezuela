"""Derivar a otra especialidad (R3 y R4 de tasks/cola-por-especialidad/spec.md).

- Desde la cola (`POST /{id}/derive`): el mismo caso, sin tomar, cambia de cola y conserva su
  hora de llegada.
- Desde el detalle (`POST /{id}/refer-to-queue`): el médico que atiende cierra su parte firmada y
  el paciente entra, en una consulta hija, a la cola del especialista, sin cita.

Los destinos deben tener médicos habilitados mirando esa cola: las especialidades de destino se
crean en cada prueba para no depender de cuántos médicos traiga el backup local.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit_log import AuditLog
from src.models.consultation import Consultation
from src.models.consultation_event import ConsultationEvent
from src.models.patient import Patient
from src.models.specialty import Specialty
from tests._helpers import GENERAL, add_doctor, auth_headers, specialty_id_by_name

PREFIX = "/api/v1"


async def _specialty(db_session: AsyncSession, *, with_doctor: bool = True) -> Specialty:
    """Especialidad nueva (y, si se pide, con un médico habilitado atendiendo su cola)."""
    specialty = Specialty(name=f"Esp prueba {uuid.uuid4().hex[:8]}")
    db_session.add(specialty)
    await db_session.flush()
    if with_doctor:
        await add_doctor(db_session, specialty=specialty.name)
    return specialty


async def _case(
    db_session: AsyncSession, specialty_id, *, email: str | None = "paciente@example.com"
) -> Consultation:
    patient = Patient(
        full_name="María Pérez",
        phone_whatsapp="+584140000222",
        email=email,
        affected_zone="Caracas",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()
    consultation = Consultation(
        code=f"TEST-{uuid.uuid4().hex[:10]}",
        patient_id=patient.id,
        specialty_id=specialty_id,
        status="waiting",
        chief_complaint="Dolor de rodilla",
        category="Lesión física",
        queued_at=datetime.now(UTC) - timedelta(hours=3),
    )
    db_session.add(consultation)
    await db_session.flush()
    return consultation


@contextmanager
def _capturar_avisos():
    enviados: list[dict] = []

    async def _fake(**kwargs) -> bool:
        enviados.append(kwargs)
        return True

    with patch("src.services.notifications.send_derivation_email", AsyncMock(side_effect=_fake)):
        yield enviados


async def _reload(db_session: AsyncSession, consultation: Consultation) -> Consultation:
    await db_session.refresh(consultation)
    return consultation


# --- Destinos ---------------------------------------------------------------------------


async def test_destinos_solo_con_medicos_y_sin_relleno(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    con_medico = await _specialty(db_session)
    sin_medico = await _specialty(db_session, with_doctor=False)
    doc = await add_doctor(db_session, specialty=GENERAL)

    resp = await client.get(
        f"{PREFIX}/consultations/derivation-targets", headers=auth_headers(doc.id)
    )
    assert resp.status_code == 200, resp.text
    names = {t["name"] for t in resp.json()}
    assert con_medico.name in names
    assert sin_medico.name not in names
    assert "Otra" not in names


async def test_un_medico_no_habilitado_no_cuenta_como_destino(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un médico con la cédula sin verificar no atiende: su especialidad no es destino."""
    specialty = await _specialty(db_session, with_doctor=False)
    await add_doctor(db_session, specialty=specialty.name, verified=False)
    doc = await add_doctor(db_session, specialty=GENERAL)

    resp = await client.get(
        f"{PREFIX}/consultations/derivation-targets", headers=auth_headers(doc.id)
    )
    assert specialty.name not in {t["name"] for t in resp.json()}


# --- Derivar desde la cola -----------------------------------------------------------------


async def test_derivar_desde_la_cola_mueve_el_mismo_caso(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    general_id = await specialty_id_by_name(db_session, GENERAL)
    destino = await _specialty(db_session)
    especialista = await add_doctor(db_session, specialty=destino.name)
    doc = await add_doctor(db_session, specialty=GENERAL)
    caso = await _case(db_session, general_id)
    llegada = caso.queued_at

    with _capturar_avisos() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations/{caso.id}/derive",
            json={"specialty_id": str(destino.id)},
            headers=auth_headers(doc.id),
        )

    assert resp.status_code == 200, resp.text
    caso = await _reload(db_session, caso)
    assert caso.specialty_id == destino.id
    assert caso.derived_from_specialty_id == general_id
    assert caso.status == "waiting" and caso.assigned_doctor_id is None
    assert caso.queued_at == llegada  # no pierde el turno

    # Sale de la cola de origen y entra a la del especialista, diciendo de dónde viene.
    origen = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    assert str(caso.id) not in {c["id"] for c in origen.json()["waiting"]}
    cola = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(especialista.id))
    item = next(c for c in cola.json()["waiting"] if c["id"] == str(caso.id))
    assert item["specialty"] == destino.name
    assert item["derived_from_specialty"] == GENERAL

    eventos = (
        await db_session.scalars(
            select(ConsultationEvent).where(ConsultationEvent.consultation_id == caso.id)
        )
    ).all()
    assert any(e.event_type == "derived" and e.created_by == doc.id for e in eventos)
    audit = (
        await db_session.scalars(
            select(AuditLog).where(
                AuditLog.action == "consultation.derived", AuditLog.resource_id == str(caso.id)
            )
        )
    ).all()
    assert len(audit) == 1

    assert len(enviados) == 1
    assert enviados[0]["to_email"] == "paciente@example.com"
    assert enviados[0]["specialty_name"] == destino.name
    assert f"/sala-espera?cid={caso.id}&t=" in enviados[0]["waiting_url"]


async def test_derivar_un_caso_que_no_es_de_tu_cola_es_403(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    destino = await _specialty(db_session)
    trauma = await add_doctor(db_session, specialty="Traumatología y ortopedia")
    # Pediatría no es ni su cola ni la de entrada.
    caso = await _case(
        db_session, await specialty_id_by_name(db_session, "Pediatría y subespecialidades")
    )

    resp = await client.post(
        f"{PREFIX}/consultations/{caso.id}/derive",
        json={"specialty_id": str(destino.id)},
        headers=auth_headers(trauma.id),
    )
    assert resp.status_code == 403, resp.text


async def test_derivar_un_caso_ya_tomado_es_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    destino = await _specialty(db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    caso = await _case(db_session, await specialty_id_by_name(db_session, GENERAL))
    await client.post(f"{PREFIX}/consultations/{caso.id}/claim", headers=auth_headers(doc.id))

    resp = await client.post(
        f"{PREFIX}/consultations/{caso.id}/derive",
        json={"specialty_id": str(destino.id)},
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 409, resp.text


async def test_destinos_invalidos_son_422(client: AsyncClient, db_session: AsyncSession) -> None:
    general_id = await specialty_id_by_name(db_session, GENERAL)
    sin_medico = await _specialty(db_session, with_doctor=False)
    doc = await add_doctor(db_session, specialty=GENERAL)
    caso = await _case(db_session, general_id)
    otra_id = await specialty_id_by_name(db_session, "Otra")

    for destino in (general_id, sin_medico.id, otra_id, uuid.uuid4()):
        resp = await client.post(
            f"{PREFIX}/consultations/{caso.id}/derive",
            json={"specialty_id": str(destino)},
            headers=auth_headers(doc.id),
        )
        assert resp.status_code == 422, (destino, resp.text)


async def test_derivar_sin_correo_del_paciente_no_intenta_avisar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    destino = await _specialty(db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    caso = await _case(db_session, await specialty_id_by_name(db_session, GENERAL), email=None)

    with _capturar_avisos() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations/{caso.id}/derive",
            json={"specialty_id": str(destino.id)},
            headers=auth_headers(doc.id),
        )
    assert resp.status_code == 200, resp.text
    assert enviados == []


# --- Derivar con especialista desde el detalle ----------------------------------------------


async def _tomado(client: AsyncClient, db_session: AsyncSession, doc_id) -> Consultation:
    caso = await _case(db_session, await specialty_id_by_name(db_session, GENERAL))
    resp = await client.post(
        f"{PREFIX}/consultations/{caso.id}/claim", headers=auth_headers(doc_id)
    )
    assert resp.status_code == 200, resp.text
    return await _reload(db_session, caso)


async def test_derivar_con_especialista_cierra_la_parte_del_medico_y_encola_una_hija(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    general_id = await specialty_id_by_name(db_session, GENERAL)
    destino = await _specialty(db_session)
    especialista = await add_doctor(db_session, specialty=destino.name)
    doc = await add_doctor(db_session, specialty=GENERAL)
    padre = await _tomado(client, db_session, doc.id)

    with _capturar_avisos() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations/{padre.id}/refer-to-queue",
            json={
                "specialty_id": str(destino.id),
                "reason": "  Sospecha de lesión de menisco  ",
                "signature": "data:image/png;base64,AAAA",
            },
            headers=auth_headers(doc.id),
        )

    assert resp.status_code == 201, resp.text
    hija_id = uuid.UUID(resp.json()["id"])

    padre = await _reload(db_session, padre)
    assert padre.status == "referred_to_specialist"
    assert padre.closed_at is not None
    assert padre.close_signature == "data:image/png;base64,AAAA"

    hija = await db_session.get(Consultation, hija_id)
    assert hija.parent_consultation_id == padre.id
    assert hija.status == "waiting" and hija.assigned_doctor_id is None
    assert hija.specialty_id == destino.id
    assert hija.derived_from_specialty_id == general_id
    assert hija.queued_at == padre.queued_at  # conserva su turno
    assert hija.video_room_url is None  # la sala se crea al tomarla
    assert hija.patient_id == padre.patient_id

    # El primer médico ya no la tiene entre sus consultas abiertas.
    panel = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    assert str(padre.id) not in {c["id"] for c in panel.json()["mine"]}

    # El especialista la ve en su cola y, en el detalle, quién la derivó y por qué.
    cola = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(especialista.id))
    assert str(hija_id) in {c["id"] for c in cola.json()["waiting"]}
    detalle = await client.get(
        f"{PREFIX}/consultations/{hija_id}", headers=auth_headers(especialista.id)
    )
    assert detalle.status_code == 200, detalle.text
    body = detalle.json()
    assert body["specialty"] == destino.name
    assert body["derived_from_specialty"] == GENERAL
    assert body["derivation"]["from_specialty"] == GENERAL
    assert body["derivation"]["by_name"] == doc.full_name
    assert body["derivation"]["reason"] == "Sospecha de lesión de menisco"

    # Al tomarla, el especialista entra con sala nueva.
    took = await client.post(
        f"{PREFIX}/consultations/{hija_id}/claim", headers=auth_headers(especialista.id)
    )
    assert took.status_code == 200, took.text
    assert "/vamed-" in took.json()["video_room_url"]

    assert len(enviados) == 1
    assert f"cid={hija_id}" in enviados[0]["waiting_url"]


async def test_solo_el_medico_que_atiende_deriva_con_especialista(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    destino = await _specialty(db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    otro = await add_doctor(db_session, specialty=GENERAL)
    padre = await _tomado(client, db_session, doc.id)

    resp = await client.post(
        f"{PREFIX}/consultations/{padre.id}/refer-to-queue",
        json={"specialty_id": str(destino.id), "reason": "motivo"},
        headers=auth_headers(otro.id),
    )
    assert resp.status_code == 409, resp.text


async def test_derivar_con_especialista_un_caso_en_cola_es_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un caso que nadie tomó se deriva desde la cola, no firmando un acto que no hubo."""
    destino = await _specialty(db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    caso = await _case(db_session, await specialty_id_by_name(db_session, GENERAL))

    resp = await client.post(
        f"{PREFIX}/consultations/{caso.id}/refer-to-queue",
        json={"specialty_id": str(destino.id), "reason": "motivo"},
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 409, resp.text


async def test_derivar_con_especialista_exige_motivo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    destino = await _specialty(db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    padre = await _tomado(client, db_session, doc.id)

    resp = await client.post(
        f"{PREFIX}/consultations/{padre.id}/refer-to-queue",
        json={"specialty_id": str(destino.id), "reason": "   "},
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 422, resp.text


async def test_un_caso_no_derivado_no_trae_bloque_de_derivacion(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    padre = await _tomado(client, db_session, doc.id)

    detalle = await client.get(f"{PREFIX}/consultations/{padre.id}", headers=auth_headers(doc.id))
    assert detalle.json()["derivation"] is None
    assert detalle.json()["specialty"] == GENERAL
