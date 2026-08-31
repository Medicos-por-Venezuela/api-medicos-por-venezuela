"""Pruebas de la interconsulta ASÍNCRONA (ver tasks/interconsulta-asincrona/spec.md).

Dos bloques:
1. Las invariantes que impone la TABLA. Pydantic protege la puerta HTTP, pero un script, una
   migración futura o un psql a mano entran por debajo; los CHECK son la última línea. Cada
   aserción corresponde a un estado imposible que, de colarse, rompería alguna de las dos vistas.
2. El flujo de crear y difundir la solicitud.
"""

import json
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.interconsultation_request import (
    REQUEST_MODES,
    REQUEST_STATUSES,
    InterconsultationRequest,
)
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.services import interconsultation_requests as requests_service
from src.services import mail as mail_service
from tests._helpers import add_doctor, auth_headers, make_profile

PREFIX = "/api/v1"
SOLICITUDES = f"{PREFIX}/interconsultation-requests"
MIS_PACIENTES = f"{PREFIX}/doctors/me/patients"


# ============================================================================
# 1. Invariantes de la tabla
# ============================================================================


async def _fixtures(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Un médico tratante, un paciente suyo y una especialidad. Devuelve sus ids."""
    doctor = make_profile(role="doctor")
    db_session.add(doctor)
    await db_session.flush()

    patient = Patient(
        full_name="Paciente de Consultorio",
        consent=True,
        created_by_doctor_id=doctor.id,
    )
    specialty = Specialty(name=f"Cardiología {uuid.uuid4().hex[:8]}")
    db_session.add_all([patient, specialty])
    await db_session.flush()
    return doctor.id, patient.id, specialty.id


def _request(doctor_id, patient_id, specialty_id, **overrides) -> InterconsultationRequest:
    fields = {
        "patient_id": patient_id,
        "requesting_doctor_id": doctor_id,
        "mode": "specialty",
        "specialty_id": specialty_id,
        "chief_complaint": "Dolor torácico atípico, ECG sin cambios.",
    }
    fields.update(overrides)
    return InterconsultationRequest(**fields)


async def _rechaza(db_session: AsyncSession, row: InterconsultationRequest) -> None:
    """La BD debe rechazar la fila. Se hace en un savepoint para no ensuciar la sesión."""
    nested = await db_session.begin_nested()
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await nested.rollback()


@pytest.mark.asyncio
async def test_solicitud_valida_nace_abierta(db_session: AsyncSession) -> None:
    """El camino feliz: sin tocar `status`, la solicitud queda esperando a que alguien la tome."""
    doctor_id, patient_id, specialty_id = await _fixtures(db_session)

    row = _request(doctor_id, patient_id, specialty_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.status == "open"
    assert row.notified_count == 0
    assert row.taken_by_doctor_id is None
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_modo_doctor_exige_destinatario(db_session: AsyncSession) -> None:
    """Una solicitud "dirigida" a nadie no le llegaría a nadie: la BD no la acepta."""
    doctor_id, patient_id, specialty_id = await _fixtures(db_session)
    await _rechaza(
        db_session,
        _request(doctor_id, patient_id, specialty_id, mode="doctor", target_doctor_id=None),
    )


@pytest.mark.asyncio
async def test_modo_especialidad_no_admite_destinatario(db_session: AsyncSession) -> None:
    """Una difusión con destinatario sería un fantasma que nadie lee: tampoco se acepta."""
    doctor_id, patient_id, specialty_id = await _fixtures(db_session)
    await _rechaza(
        db_session,
        _request(
            doctor_id, patient_id, specialty_id, mode="specialty", target_doctor_id=uuid.uuid4()
        ),
    )


@pytest.mark.asyncio
async def test_modo_desconocido_rechazado(db_session: AsyncSession) -> None:
    doctor_id, patient_id, specialty_id = await _fixtures(db_session)
    await _rechaza(db_session, _request(doctor_id, patient_id, specialty_id, mode="urgente"))


@pytest.mark.asyncio
async def test_estado_desconocido_rechazado(db_session: AsyncSession) -> None:
    """Los cuatro estados de la máquina y nada más (open/taken/closed/cancelled)."""
    doctor_id, patient_id, specialty_id = await _fixtures(db_session)
    await _rechaza(db_session, _request(doctor_id, patient_id, specialty_id, status="archivada"))


@pytest.mark.asyncio
async def test_tomada_sin_fecha_es_estado_imposible(db_session: AsyncSession) -> None:
    """`taken_by_doctor_id` y `taken_at` van juntos o no van: un caso tomado sin especialista
    (o sin cuándo) dejaría al tratante viendo una toma que no puede atribuir a nadie."""
    doctor_id, patient_id, specialty_id = await _fixtures(db_session)
    await _rechaza(
        db_session,
        _request(
            doctor_id,
            patient_id,
            specialty_id,
            status="taken",
            taken_by_doctor_id=uuid.uuid4(),
            taken_at=None,
        ),
    )


def test_catalogos_del_modelo_son_los_de_la_maquina_de_estados() -> None:
    """Los conjuntos del modelo son el espejo de los CHECK; si alguien agrega un estado en la
    BD sin actualizar acá (o al revés), esto lo caza antes que un 500 en producción."""
    assert REQUEST_STATUSES == {"open", "taken", "closed", "cancelled"}
    assert REQUEST_MODES == {"specialty", "doctor"}


# ============================================================================
# 2. Crear la solicitud y difundirla
# ============================================================================


@pytest_asyncio.fixture
async def sin_correo(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Intercepta el fan-out: registra con qué se llamó a `send_bulk`, sin tocar la red.

    Se parchea en el ROUTER porque ahí se importó el nombre (`from ... import send_bulk`), que
    es lo que BackgroundTasks acaba invocando."""
    llamadas: list[dict] = []

    async def fake_send_bulk(**kwargs) -> int:
        llamadas.append(kwargs)
        return len(kwargs.get("recipients", []))

    import src.routers.interconsultation_requests as router_mod

    monkeypatch.setattr(router_mod, "send_bulk", fake_send_bulk)
    return llamadas


async def _especialidad_pedible(
    db_session: AsyncSession, nombre: str = "Cardiología"
) -> Specialty:
    sp = Specialty(name=f"{nombre} {uuid.uuid4().hex[:8]}")
    db_session.add(sp)
    await db_session.flush()
    return sp


async def _medico(
    db_session: AsyncSession, especialidad: Specialty | None = None, **kw
) -> Profile:
    """Médico habilitado CON email: `make_profile` no lo pone, y sin email el fan-out lo
    descarta (no hay a dónde escribirle)."""
    medico = await add_doctor(db_session, **kw)
    medico.email = f"medico-{uuid.uuid4().hex[:10]}@example.com"
    if especialidad is not None:
        medico.specialty_id = especialidad.id
    await db_session.flush()
    return medico


async def _paciente_de(client: AsyncClient, medico: Profile) -> str:
    creado = await client.post(
        MIS_PACIENTES,
        json={"full_name": "Paciente de Consultorio", "age_range": "40-49", "consent": True},
        headers=auth_headers(medico.id),
    )
    assert creado.status_code == 201, creado.text
    return creado.json()["id"]


def _payload(patient_id: str, especialidad, **extra) -> dict:
    """`extra` puede pisar cualquier clave — incluida `specialty_id`, que el modo 'doctor'
    manda en None. Por eso el parámetro NO se llama specialty_id: chocaría con el kwarg."""
    base = {
        "patient_id": patient_id,
        "mode": "specialty",
        "specialty_id": str(especialidad),
        "chief_complaint": "Dolor torácico atípico de dos semanas, ECG sin cambios agudos.",
    }
    base.update(extra)
    return base


async def test_por_especialidad_difunde_a_los_medicos_de_esa_especialidad(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    tratante = await _medico(db_session)
    cardio_1 = await _medico(db_session, sp)
    cardio_2 = await _medico(db_session, sp)
    await db_session.flush()

    paciente = await _paciente_de(client, tratante)
    creada = await client.post(
        SOLICITUDES, json=_payload(paciente, sp.id), headers=auth_headers(tratante.id)
    )
    assert creada.status_code == 201, creada.text
    body = creada.json()
    assert body["status"] == "open"
    assert body["notified_count"] == 2
    assert body["specialty_id"] == str(sp.id)

    # El correo se encoló con los dos destinatarios y SIN identidad del paciente.
    assert len(sin_correo) == 1
    difusion = sin_correo[0]
    assert set(difusion["recipients"]) == {cardio_1.email, cardio_2.email}
    assert "Paciente de Consultorio" not in difusion["text"]
    assert "Paciente de Consultorio" not in difusion["html"]


async def test_el_solicitante_no_se_notifica_a_si_mismo(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """Avisarle de su propio caso sería ruido, y encima le aparecería en su bandeja."""
    sp = await _especialidad_pedible(db_session)
    tratante = await _medico(db_session, sp)  # él MISMO es de la especialidad que pide
    otro = await _medico(db_session, sp)
    await db_session.flush()

    paciente = await _paciente_de(client, tratante)
    creada = await client.post(
        SOLICITUDES, json=_payload(paciente, sp.id), headers=auth_headers(tratante.id)
    )
    assert creada.json()["notified_count"] == 1
    assert sin_correo[0]["recipients"] == [otro.email]


async def test_no_se_difunde_a_medicos_no_habilitados(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """No se le manda un caso a quien el backend no dejaría atenderlo: mismo criterio que el
    gate de credencial médica."""
    sp = await _especialidad_pedible(db_session)
    tratante = await _medico(db_session)
    habilitado = await _medico(db_session, sp)
    _sin_credencial = await _medico(db_session, sp, verified=False)
    inactivo = await _medico(db_session, sp)
    inactivo.active = False
    await db_session.flush()

    paciente = await _paciente_de(client, tratante)
    creada = await client.post(
        SOLICITUDES, json=_payload(paciente, sp.id), headers=auth_headers(tratante.id)
    )
    assert creada.json()["notified_count"] == 1
    assert sin_correo[0]["recipients"] == [habilitado.email]


async def test_respeta_el_opt_out_de_notificaciones(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    tratante = await _medico(db_session)
    quiere = await _medico(db_session, sp)
    no_quiere = await _medico(db_session, sp)
    no_quiere.notification_prefs = {requests_service.BROADCAST_EVENT: {"email": False}}
    await db_session.flush()

    paciente = await _paciente_de(client, tratante)
    creada = await client.post(
        SOLICITUDES, json=_payload(paciente, sp.id), headers=auth_headers(tratante.id)
    )
    assert creada.json()["notified_count"] == 1
    assert sin_correo[0]["recipients"] == [quiere.email]


async def test_especialidad_no_pedible_es_rechazada(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """La regla sale del flag del catálogo, no de comparar el nombre."""
    sp = await _especialidad_pedible(db_session, "Especialidad Apagada")
    sp.available_for_interconsultation = False
    tratante = await _medico(db_session)
    await db_session.flush()

    paciente = await _paciente_de(client, tratante)
    rechazada = await client.post(
        SOLICITUDES, json=_payload(paciente, sp.id), headers=auth_headers(tratante.id)
    )
    assert rechazada.status_code == 422, rechazada.text
    assert sin_correo == []  # ni se encoló el correo


async def test_no_se_puede_pedir_sobre_un_paciente_ajeno(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """IDOR: el caso tiene que ser de un paciente propio."""
    sp = await _especialidad_pedible(db_session)
    dueno = await _medico(db_session)
    intruso = await _medico(db_session)
    await db_session.flush()

    ajeno = await _paciente_de(client, dueno)
    intento = await client.post(
        SOLICITUDES, json=_payload(ajeno, sp.id), headers=auth_headers(intruso.id)
    )
    assert intento.status_code == 403, intento.text
    assert sin_correo == []


async def test_modo_doctor_deriva_la_especialidad_y_avisa_solo_a_uno(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    tratante = await _medico(db_session)
    elegido = await _medico(db_session, sp)
    elegido.whatsapp_number = "+58412555111"
    _otro_de_la_misma = await _medico(db_session, sp)
    await db_session.flush()

    paciente = await _paciente_de(client, tratante)
    creada = await client.post(
        SOLICITUDES,
        json=_payload(
            paciente, sp.id, mode="doctor", specialty_id=None, target_doctor_id=str(elegido.id)
        ),
        headers=auth_headers(tratante.id),
    )
    assert creada.status_code == 201, creada.text
    body = creada.json()
    assert body["mode"] == "doctor"
    assert body["specialty_id"] == str(sp.id)  # derivada de la ficha del elegido
    assert body["notified_count"] == 1
    # El tratante lo eligió: ve su teléfono sin esperar a que tome el caso.
    assert body["target_doctor"]["whatsapp_number"] == "+58412555111"
    assert sin_correo[0]["recipients"] == [elegido.email]


async def test_modo_doctor_exige_target_y_prohibe_specialty_id(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """El mismo invariante que el CHECK de la BD, devuelto como 422 explicativo."""
    sp = await _especialidad_pedible(db_session)
    tratante = await _medico(db_session)
    await db_session.flush()
    paciente = await _paciente_de(client, tratante)
    headers = auth_headers(tratante.id)

    sin_target = await client.post(
        SOLICITUDES,
        json=_payload(paciente, sp.id, mode="doctor", specialty_id=None),
        headers=headers,
    )
    assert sin_target.status_code == 422

    con_ambos = await client.post(
        SOLICITUDES,
        json=_payload(paciente, sp.id, mode="doctor", target_doctor_id=str(uuid.uuid4())),
        headers=headers,
    )
    assert con_ambos.status_code == 422


async def test_no_se_puede_pedir_a_uno_mismo(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    tratante = await _medico(db_session, sp)
    await db_session.flush()

    paciente = await _paciente_de(client, tratante)
    intento = await client.post(
        SOLICITUDES,
        json=_payload(
            paciente, sp.id, mode="doctor", specialty_id=None, target_doctor_id=str(tratante.id)
        ),
        headers=auth_headers(tratante.id),
    )
    assert intento.status_code == 409, intento.text


async def test_mis_solicitudes_solo_devuelve_las_propias(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    a = await _medico(db_session)
    b = await _medico(db_session)
    await db_session.flush()

    de_a = await client.post(
        SOLICITUDES,
        json=_payload(await _paciente_de(client, a), sp.id),
        headers=auth_headers(a.id),
    )
    de_b = await client.post(
        SOLICITUDES,
        json=_payload(await _paciente_de(client, b), sp.id),
        headers=auth_headers(b.id),
    )
    assert (de_a.status_code, de_b.status_code) == (201, 201)

    mias = await client.get(f"{SOLICITUDES}/mine", headers=auth_headers(a.id))
    assert mias.status_code == 200, mias.text
    ids = {s["id"] for s in mias.json()}
    assert de_a.json()["id"] in ids
    assert de_b.json()["id"] not in ids


async def test_un_fallo_de_correo_no_deshace_la_solicitud(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mailtrap caído no puede tumbar la solicitud: el commit va ANTES del envío y `send_bulk`
    absorbe el fallo. Se rompe el cliente de verdad, no `send_bulk` — doblar la función que
    justamente promete no lanzar probaría la promesa contra sí misma."""

    class MailtrapCaido:
        def send(self, mail: object) -> dict:
            raise RuntimeError("mailtrap caído")

    monkeypatch.setattr(settings, "MAILTRAP_API_TOKEN", "token-de-prueba")
    monkeypatch.setattr(settings, "MAILTRAP_INBOX_ID", None)
    monkeypatch.setattr(mail_service, "_bulk_client", MailtrapCaido)

    sp = await _especialidad_pedible(db_session)
    tratante = await _medico(db_session)
    await _medico(db_session, sp)  # un destinatario, para que el envío se intente de verdad
    await db_session.flush()

    creada = await client.post(
        SOLICITUDES,
        json=_payload(await _paciente_de(client, tratante), sp.id),
        headers=auth_headers(tratante.id),
    )
    assert creada.status_code == 201, creada.text
    assert creada.json()["notified_count"] == 1  # se contó al destinatario elegible

    mias = await client.get(f"{SOLICITUDES}/mine", headers=auth_headers(tratante.id))
    assert creada.json()["id"] in {s["id"] for s in mias.json()}


async def test_paciente_no_puede_pedir_interconsultas(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El feature es entre médicos: una cuenta de paciente no tiene el permiso."""
    sp = await _especialidad_pedible(db_session)
    paciente_user = make_profile(role="patient")
    db_session.add(paciente_user)
    await db_session.flush()

    intento = await client.post(
        SOLICITUDES,
        json=_payload(str(uuid.uuid4()), sp.id),
        headers=auth_headers(paciente_user.id),
    )
    assert intento.status_code == 403, intento.text


# ============================================================================
# 3. Bandeja anonimizada, tomar el caso y cerrarlo
# ============================================================================

# PII del paciente que NUNCA puede aparecer en lo que ve el especialista. Los valores son
# rarísimos a propósito: así la búsqueda en el JSON crudo no da falsos positivos.
PII_PACIENTE = {
    "full_name": "Zoraida Quispecallo Mirabal",
    "cedula": "V-98765432",
    "phone_whatsapp": "+58412ZZZ9999",
    "affected_zone": "Zulia Municipio Inventado",
    "description": "Antecedente confidencial del paciente que no debe salir.",
}


async def _caso_abierto(
    client: AsyncClient, db_session: AsyncSession, sp: Specialty
) -> tuple[Profile, str]:
    """Un tratante con PII cargada al máximo y una solicitud abierta. Devuelve (tratante, id)."""
    tratante = await _medico(db_session)
    creado = await client.post(
        MIS_PACIENTES,
        json={**PII_PACIENTE, "age_range": "60-69", "consent": True},
        headers=auth_headers(tratante.id),
    )
    assert creado.status_code == 201, creado.text
    solicitud = await client.post(
        SOLICITUDES,
        json=_payload(creado.json()["id"], sp.id),
        headers=auth_headers(tratante.id),
    )
    assert solicitud.status_code == 201, solicitud.text
    return tratante, solicitud.json()["id"]


def _pii_en(payload: object) -> list[str]:
    """Qué valores prohibidos aparecen en el JSON CRUDO. Se busca sobre el texto serializado,
    no campo a campo: así también caza la PII que se cuele dentro de un objeto anidado."""
    crudo = json.dumps(payload, ensure_ascii=False)
    return [valor for valor in PII_PACIENTE.values() if valor in crudo]


async def test_la_bandeja_no_filtra_pii_del_paciente_ni_identidad_del_tratante(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """La frontera de datos del feature. Si esto falla, el flujo entero pierde su sentido:
    el especialista debe decidir por el CASO, no por quién es el paciente ni quién pregunta."""
    sp = await _especialidad_pedible(db_session)
    tratante, _ = await _caso_abierto(client, db_session, sp)
    especialista = await _medico(db_session, sp)
    await db_session.flush()

    bandeja = await client.get(f"{SOLICITUDES}/inbox", headers=auth_headers(especialista.id))
    assert bandeja.status_code == 200, bandeja.text
    caso = bandeja.json()[0]

    assert _pii_en(bandeja.json()) == []
    # Tampoco quién pide: ni su nombre, ni su id, ni su contacto.
    crudo = json.dumps(bandeja.json(), ensure_ascii=False)
    assert tratante.full_name not in crudo
    assert str(tratante.id) not in crudo
    assert tratante.email not in crudo
    # Lo que sí necesita para decidir.
    assert caso["chief_complaint"]
    assert caso["patient_age_range"] == "60-69"
    assert caso["specialty_name"] == sp.name


async def test_la_bandeja_solo_muestra_lo_que_puedo_tomar(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """De mi especialidad y dirigidas a mí. Ni de otra especialidad, ni las mías propias, ni
    las dirigidas a otro colega aunque compartamos especialidad."""
    sp = await _especialidad_pedible(db_session)
    otra_sp = await _especialidad_pedible(db_session, "Neurología")
    especialista = await _medico(db_session, sp)
    colega = await _medico(db_session, sp)
    await db_session.flush()

    _, mio_de_mi_especialidad = await _caso_abierto(client, db_session, sp)
    _, de_otra_especialidad = await _caso_abierto(client, db_session, otra_sp)

    # Una dirigida al colega, no a mí.
    tratante = await _medico(db_session)
    await db_session.flush()
    paciente = await _paciente_de(client, tratante)
    dirigida_al_colega = await client.post(
        SOLICITUDES,
        json=_payload(
            paciente, sp.id, mode="doctor", specialty_id=None, target_doctor_id=str(colega.id)
        ),
        headers=auth_headers(tratante.id),
    )
    assert dirigida_al_colega.status_code == 201, dirigida_al_colega.text

    # Y una que pedí yo mismo.
    mi_paciente = await _paciente_de(client, especialista)
    propia = await client.post(
        SOLICITUDES, json=_payload(mi_paciente, sp.id), headers=auth_headers(especialista.id)
    )
    assert propia.status_code == 201

    bandeja = await client.get(f"{SOLICITUDES}/inbox", headers=auth_headers(especialista.id))
    ids = {c["id"] for c in bandeja.json()}
    assert mio_de_mi_especialidad in ids
    assert de_otra_especialidad not in ids
    assert dirigida_al_colega.json()["id"] not in ids
    assert propia.json()["id"] not in ids


async def test_una_dirigida_a_mi_aparece_marcada(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    elegido = await _medico(db_session, sp)
    tratante = await _medico(db_session)
    await db_session.flush()

    paciente = await _paciente_de(client, tratante)
    creada = await client.post(
        SOLICITUDES,
        json=_payload(
            paciente, sp.id, mode="doctor", specialty_id=None, target_doctor_id=str(elegido.id)
        ),
        headers=auth_headers(tratante.id),
    )
    assert creada.status_code == 201

    bandeja = await client.get(f"{SOLICITUDES}/inbox", headers=auth_headers(elegido.id))
    caso = next(c for c in bandeja.json() if c["id"] == creada.json()["id"])
    assert caso["dirigida_a_mi"] is True


async def test_tomar_entrega_el_contacto_del_tratante_y_nunca_del_paciente(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """El objetivo del flujo: que los dos médicos se hablen. Pero la PII del paciente sigue
    sin aparecer NI DESPUÉS de tomar el caso."""
    sp = await _especialidad_pedible(db_session)
    tratante, request_id = await _caso_abierto(client, db_session, sp)
    tratante.whatsapp_number = "+58412000777"
    especialista = await _medico(db_session, sp)
    await db_session.flush()

    tomada = await client.post(
        f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(especialista.id)
    )
    assert tomada.status_code == 200, tomada.text
    body = tomada.json()

    assert body["status"] == "taken"
    assert body["taken_at"] is not None
    assert body["requesting_doctor"]["whatsapp_number"] == "+58412000777"
    assert body["requesting_doctor"]["full_name"] == tratante.full_name
    assert _pii_en(body) == []  # el paciente sigue siendo anónimo


async def test_un_medico_de_otra_especialidad_no_puede_tomar(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    otra_sp = await _especialidad_pedible(db_session, "Neurología")
    _, request_id = await _caso_abierto(client, db_session, sp)
    ajeno = await _medico(db_session, otra_sp)
    await db_session.flush()

    intento = await client.post(f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(ajeno.id))
    assert intento.status_code == 403, intento.text


async def test_una_dirigida_a_otro_no_la_puede_tomar_un_tercero(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """En modo 'doctor' el destinatario es el elegido, no cualquiera de la especialidad."""
    sp = await _especialidad_pedible(db_session)
    elegido = await _medico(db_session, sp)
    tercero = await _medico(db_session, sp)
    tratante = await _medico(db_session)
    await db_session.flush()

    paciente = await _paciente_de(client, tratante)
    creada = await client.post(
        SOLICITUDES,
        json=_payload(
            paciente, sp.id, mode="doctor", specialty_id=None, target_doctor_id=str(elegido.id)
        ),
        headers=auth_headers(tratante.id),
    )
    request_id = creada.json()["id"]

    assert (
        await client.post(f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(tercero.id))
    ).status_code == 403
    assert (
        await client.post(f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(elegido.id))
    ).status_code == 200


async def test_no_se_puede_tomar_una_ya_tomada(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    _, request_id = await _caso_abierto(client, db_session, sp)
    primero = await _medico(db_session, sp)
    segundo = await _medico(db_session, sp)
    await db_session.flush()

    assert (
        await client.post(f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(primero.id))
    ).status_code == 200
    tarde = await client.post(f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(segundo.id))
    assert tarde.status_code == 409, tarde.text


async def test_el_tratante_recibe_correo_cuando_toman_su_caso(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict], monkeypatch
) -> None:
    avisos: list[dict] = []

    async def fake_send_mail(**kwargs) -> bool:
        avisos.append(kwargs)
        return True

    import src.routers.interconsultation_requests as router_mod

    monkeypatch.setattr(router_mod, "send_mail", fake_send_mail)

    sp = await _especialidad_pedible(db_session)
    tratante, request_id = await _caso_abierto(client, db_session, sp)
    especialista = await _medico(db_session, sp)
    await db_session.flush()

    await client.post(f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(especialista.id))

    assert len(avisos) == 1
    assert avisos[0]["to_email"] == tratante.email
    assert especialista.full_name in avisos[0]["text"]
    assert "/panel-medico" in avisos[0]["text"]
    # Ni siquiera al tratante se le manda por correo la PII de su paciente.
    assert PII_PACIENTE["cedula"] not in avisos[0]["text"]


async def test_casos_que_tome_conserva_el_contacto(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """Sin esta lista, el especialista pierde el WhatsApp del tratante al recargar."""
    sp = await _especialidad_pedible(db_session)
    tratante, request_id = await _caso_abierto(client, db_session, sp)
    tratante.whatsapp_number = "+58412111333"
    especialista = await _medico(db_session, sp)
    otro = await _medico(db_session, sp)
    await db_session.flush()

    await client.post(f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(especialista.id))

    mios = await client.get(f"{SOLICITUDES}/taken-by-me", headers=auth_headers(especialista.id))
    assert mios.status_code == 200, mios.text
    assert [c["id"] for c in mios.json()] == [request_id]
    assert mios.json()[0]["requesting_doctor"]["whatsapp_number"] == "+58412111333"
    assert _pii_en(mios.json()) == []

    # Y no aparece en la lista de otro especialista.
    ajenos = await client.get(f"{SOLICITUDES}/taken-by-me", headers=auth_headers(otro.id))
    assert ajenos.json() == []


async def test_el_tratante_ve_al_especialista_en_mis_solicitudes(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    tratante, request_id = await _caso_abierto(client, db_session, sp)
    especialista = await _medico(db_session, sp)
    especialista.whatsapp_number = "+58414999888"
    await db_session.flush()

    await client.post(f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(especialista.id))

    mias = await client.get(f"{SOLICITUDES}/mine", headers=auth_headers(tratante.id))
    caso = next(s for s in mias.json() if s["id"] == request_id)
    assert caso["status"] == "taken"
    assert caso["taken_by"]["full_name"] == especialista.full_name
    assert caso["taken_by"]["whatsapp_number"] == "+58414999888"


# --- Transiciones terminales ---


async def test_cancelar_una_abierta_y_no_una_tomada(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    tratante, request_id = await _caso_abierto(client, db_session, sp)
    especialista = await _medico(db_session, sp)
    await db_session.flush()

    cancelada = await client.post(
        f"{SOLICITUDES}/{request_id}/cancel", headers=auth_headers(tratante.id)
    )
    assert cancelada.status_code == 200, cancelada.text
    assert cancelada.json()["status"] == "cancelled"
    assert cancelada.json()["cancelled_at"] is not None

    # Ya no se puede tomar, ni cancelar de nuevo.
    assert (
        await client.post(
            f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(especialista.id)
        )
    ).status_code == 409
    assert (
        await client.post(f"{SOLICITUDES}/{request_id}/cancel", headers=auth_headers(tratante.id))
    ).status_code == 409


async def test_cancelar_una_ajena_es_403(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    _, request_id = await _caso_abierto(client, db_session, sp)
    intruso = await _medico(db_session)
    await db_session.flush()

    intento = await client.post(
        f"{SOLICITUDES}/{request_id}/cancel", headers=auth_headers(intruso.id)
    )
    assert intento.status_code == 403, intento.text


async def test_cierra_el_tratante_y_NO_el_especialista(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    """La regla que pidió el usuario, y la que un refactor futuro rompería sin darse cuenta:
    el especialista puede tomar el caso, pero cerrarlo es del médico tratante."""
    sp = await _especialidad_pedible(db_session)
    tratante, request_id = await _caso_abierto(client, db_session, sp)
    especialista = await _medico(db_session, sp)
    await db_session.flush()

    await client.post(f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(especialista.id))

    # El especialista NO puede cerrar, aunque sea quien tomó el caso.
    del_especialista = await client.post(
        f"{SOLICITUDES}/{request_id}/close", headers=auth_headers(especialista.id)
    )
    assert del_especialista.status_code == 403, del_especialista.text

    # El tratante sí.
    cerrada = await client.post(
        f"{SOLICITUDES}/{request_id}/close",
        json={"closing_note": "Resuelto por teléfono, se ajustó el tratamiento."},
        headers=auth_headers(tratante.id),
    )
    assert cerrada.status_code == 200, cerrada.text
    assert cerrada.json()["status"] == "closed"
    assert cerrada.json()["closed_at"] is not None


async def test_no_se_cierra_una_que_nadie_tomo(
    client: AsyncClient, db_session: AsyncSession, sin_correo: list[dict]
) -> None:
    sp = await _especialidad_pedible(db_session)
    tratante, request_id = await _caso_abierto(client, db_session, sp)
    await db_session.flush()

    intento = await client.post(
        f"{SOLICITUDES}/{request_id}/close", headers=auth_headers(tratante.id)
    )
    assert intento.status_code == 409, intento.text
