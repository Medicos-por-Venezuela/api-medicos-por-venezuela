"""Pruebas de la interconsulta ASÍNCRONA (ver tasks/interconsulta-asincrona/spec.md).

Dos bloques:
1. Las invariantes que impone la TABLA. Pydantic protege la puerta HTTP, pero un script, una
   migración futura o un psql a mano entran por debajo; los CHECK son la última línea. Cada
   aserción corresponde a un estado imposible que, de colarse, rompería alguna de las dos vistas.
2. El flujo de crear y difundir la solicitud.
"""

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
