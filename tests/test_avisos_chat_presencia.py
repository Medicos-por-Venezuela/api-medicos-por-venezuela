"""Avisos del chat y presencia del paciente — `tasks/avisos-chat-presencia/spec.md`.

Cuatro features del chat, cuatro bloques:

- R1: el correo al paciente nombra a su médico y distingue primer contacto de respuesta.
- R2: la presencia sobrevive al reinicio del proceso y a la derivación.
- R3: no se le escribe a un paciente que está leyendo; si no lee, el barrido sí le escribe.
- R4: una sola regla de anti-ráfaga, en la base, compartida.

Ninguna prueba envía correo de verdad: `mail_enabled()` es False sin `MAILTRAP_API_TOKEN` y
además se dobla `notifications.send_mail` para capturar destinatario, asunto y cuerpo.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import consultation_token
from src.core.config import settings
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.profile import Profile
from src.services import notifications, presence, registration_mail
from tests._helpers import (
    add_doctor,
    any_specialty_id,
    auth_headers,
    valid_patient_payload,
)

PREFIX = "/api/v1"


# =====================================================================
# Helpers
# =====================================================================


async def _caso(
    client: AsyncClient,
    db: AsyncSession,
    *,
    doctor: Profile,
    patient_email: str | None = "paciente@example.com",
) -> tuple[Consultation, Patient, str]:
    """Paciente + consulta en curso asignada a `doctor`. Devuelve (consulta, paciente, token)."""
    p_resp = await client.post(f"{PREFIX}/patients", json=valid_patient_payload())
    assert p_resp.status_code == 201, p_resp.text
    patient = await db.get(Patient, uuid.UUID(p_resp.json()["id"]))
    assert patient is not None
    patient.email = patient_email

    c_resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": str(patient.id),
            "specialty_id": await any_specialty_id(client),
            "chief_complaint": "Motivo clínico inicial",
        },
    )
    assert c_resp.status_code == 201, c_resp.text
    consultation = await db.get(Consultation, uuid.UUID(c_resp.json()["id"]))
    assert consultation is not None
    consultation.status = "in_progress"
    consultation.assigned_doctor_id = doctor.id
    await db.flush()
    return consultation, patient, consultation_token.issue(consultation.id)


@pytest.fixture
def correos_capturados(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Dobla el envío y acumula lo que habría salido."""
    salidos: list[dict] = []

    async def _fake(
        to_email: str,
        subject: str,
        text: str,
        html: str = "",
        category: str = "general",
        **_: object,
    ) -> bool:
        salidos.append(
            {"to": to_email, "subject": subject, "text": text, "html": html, "category": category}
        )
        return True

    monkeypatch.setattr(notifications, "send_mail", _fake)
    return salidos


@pytest.fixture(autouse=True)
def presencia_limpia():
    """Cada prueba arranca sin presencia heredada: el registro en vivo es global al proceso y
    una prueba que deje a un paciente "en línea" cambiaría el veredicto de la siguiente."""
    presence._ACTIVE_PATIENTS.clear()
    yield
    presence._ACTIVE_PATIENTS.clear()


def _offline(consultation: Consultation) -> None:
    """Deja al paciente claramente desconectado por las dos vías."""
    presence._ACTIVE_PATIENTS.pop(consultation.id, None)
    consultation.patient_last_seen_at = None


# =====================================================================
# R1 — El correo nombra al médico y distingue primer contacto
# =====================================================================


def test_correo_paciente_nombra_al_medico_y_distingue_primer_contacto() -> None:
    """CA1.1 y CA1.2: cuatro asuntos distinguibles por nombre y por primer contacto."""
    cid = uuid.uuid4()

    primero, texto_primero, html_primero = notifications.patient_message_received_email(
        cid, "MPV-1", doctor_name="Ana Pérez", is_first_contact=True
    )
    assert primero == "Ana Pérez te escribió por tu caso"
    assert "ha revisado tu caso" in texto_primero
    assert "Ana Pérez" in html_primero

    respuesta, texto_respuesta, _ = notifications.patient_message_received_email(
        cid, "MPV-1", doctor_name="Ana Pérez", is_first_contact=False
    )
    assert respuesta == "Ana Pérez te respondió"
    assert "ha revisado tu caso" not in texto_respuesta

    # Sin nombre cae a "Tu médico", nunca a un asunto roto con None.
    sin_nombre, texto_sin_nombre, _ = notifications.patient_message_received_email(
        cid, "MPV-1", is_first_contact=True
    )
    assert sin_nombre == "Tu médico te escribió por tu caso"
    assert "None" not in texto_sin_nombre

    sin_nombre_resp, _, _ = notifications.patient_message_received_email(cid, "MPV-1")
    assert sin_nombre_resp == "Tu médico te respondió"


def test_correo_paciente_escapa_el_nombre_del_medico() -> None:
    """CA1.1: `full_name` lo teclea el médico. Sin escapar, un nombre con HTML mete un enlace
    vivo en un correo que la plataforma sí envió — phishing servido por nosotros."""
    _, _, html = notifications.patient_message_received_email(
        uuid.uuid4(), "MPV-1", doctor_name='<a href="http://malo/">Aprobar</a>'
    )
    assert '<a href="http://malo/">' not in html
    assert "&lt;a href=" in html


def test_pii_el_aviso_no_lleva_cuerpo_ni_adjuntos() -> None:
    """CA1.3: aserción NEGATIVA. Si alguien añade "contexto" al correo, esto falla."""
    _, texto, html = notifications.patient_message_received_email(
        uuid.uuid4(), "MPV-1", doctor_name="Ana Pérez", is_first_contact=True
    )
    for prohibido in (
        "tómese el medicamento",
        "resultados-laboratorio.pdf",
        "alergia",
        "diagnóstico",
    ):
        assert prohibido not in texto
        assert prohibido not in html


async def test_args_resuelven_nombre_y_primer_contacto(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA1.2/T3: el primer mensaje del médico es «primer contacto»; tras escribir el paciente,
    deja de serlo."""
    doc = await add_doctor(db_session)
    doc.full_name = "Ana Pérez"
    consultation, _, token = await _caso(client, db_session, doctor=doc)
    _offline(consultation)

    args = await notifications.message_received_mail_args(
        db_session, consultation.id, uuid.uuid4(), "doctor_to_patient"
    )
    assert args is not None
    assert args["subject"] == "Ana Pérez te escribió por tu caso"

    # El paciente escribe: a partir de aquí el médico "responde".
    resp = await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Gracias doctora."},
    )
    assert resp.status_code == 201
    notifications.clear_notice_state(consultation, "patient")
    _offline(consultation)

    args2 = await notifications.message_received_mail_args(
        db_session, consultation.id, uuid.uuid4(), "doctor_to_patient"
    )
    assert args2 is not None
    assert args2["subject"] == "Ana Pérez te respondió"


# =====================================================================
# R2 — Presencia persistente y por cadena
# =====================================================================


async def test_presencia_throttle_no_escribe_dos_veces_seguidas(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA2.2: la memoria se escribe siempre; la columna, como mucho una vez por ventana.

    Sin el throttle, el stream de la sala escribiría esta fila cada 4 s por paciente esperando,
    sobre una tabla con CDC hacia el Board."""
    doc = await add_doctor(db_session)
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    _offline(consultation)

    assert presence.touch_patient_presence(consultation) is True
    primera = consultation.patient_last_seen_at
    assert primera is not None

    # Inmediatamente después: la columna no se toca, la memoria sí sigue fresca.
    assert presence.touch_patient_presence(consultation) is False
    assert consultation.patient_last_seen_at == primera
    assert presence.is_patient_online(consultation.id, consultation.patient_last_seen_at) is True

    # Pasada la ventana, vuelve a escribir.
    consultation.patient_last_seen_at = datetime.now(UTC) - timedelta(
        seconds=settings.MESSAGING_PRESENCE_PERSIST_SECONDS + 1
    )
    assert presence.touch_patient_presence(consultation) is True
    assert consultation.patient_last_seen_at != primera


async def test_presencia_sobrevive_al_reinicio_del_proceso(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA2.1/CA2.4: con el registro en vivo vaciado (lo que pasa en un reinicio o en otra
    réplica), `patient_last_seen_at` sostiene el «En línea» del buzón del médico."""
    doc = await add_doctor(db_session)
    consultation, _, token = await _caso(client, db_session, doctor=doc)

    resp = await anon_client.get(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers={"X-Consultation-Token": token},
    )
    assert resp.status_code == 200
    await db_session.refresh(consultation)
    assert consultation.patient_last_seen_at is not None

    # Simula el reinicio: solo queda la columna.
    presence._ACTIVE_PATIENTS.clear()
    assert presence.is_patient_online(consultation.id, consultation.patient_last_seen_at) is True


async def test_presencia_del_paciente_derivado_va_al_caso_vigente(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA2.3: el stream del paciente derivado marca presencia en la consulta HIJA.

    Es el bug que no se ve en desarrollo: hacen falta una cadena de dos consultas y dos médicos.
    Con el id pedido, el especialista que lo tiene en cola lo veía «Desconectado» teniéndolo
    delante de la pantalla.
    """
    doc = await add_doctor(db_session)
    padre, patient, token = await _caso(client, db_session, doctor=doc)

    hija = Consultation(
        id=uuid.uuid4(),
        patient_id=patient.id,
        specialty_id=padre.specialty_id,
        status="waiting",
        parent_consultation_id=padre.id,
    )
    db_session.add(hija)
    await db_session.flush()

    # El stream SSE pasa por aquí en cada ciclo, con el id del caso vigente de la cadena.
    from src.services import waiting_room

    vigente = await waiting_room.current_in_chain(db_session, padre.id)
    assert vigente.id == hija.id
    assert await presence.persist_patient_presence(db_session, vigente.id) is True

    await db_session.refresh(hija)
    assert hija.patient_last_seen_at is not None
    assert presence.is_patient_online(hija.id, hija.patient_last_seen_at) is True
    # Y la presencia NO se le atribuye al caso padre, que ya no es el que espera a nadie.
    await db_session.refresh(padre)
    assert padre.patient_last_seen_at is None

    # Comprobación de extremo a extremo: el buzón del médico de la hija lo ve en línea.
    hija.assigned_doctor_id = doc.id
    hija.status = "in_progress"
    await db_session.flush()
    await anon_client.post(
        f"{PREFIX}/consultations/{hija.id}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Buenas tardes."},
    )
    inbox = await anon_client.get(f"{PREFIX}/inbox", headers=auth_headers(doc.id))
    assert inbox.status_code == 200
    hilos = {item["consultation_id"]: item for item in inbox.json()}
    assert hilos[str(hija.id)]["patient_online"] is True


async def test_sse_llama_a_touch_presence_en_cada_ciclo() -> None:
    """T7: `sse_events` sigue siendo pura — recibe la presencia inyectada, sin base ni reloj."""
    from src.schemas.consultation import WaitingRoomResponse
    from src.services import waiting_room

    cid = uuid.uuid4()
    llamadas: list[int] = []
    reloj = iter([0.0, 0.0, 1.0, 1.0, 999.0, 999.0])

    async def fetch() -> WaitingRoomResponse:
        return WaitingRoomResponse(
            consultation_id=cid, code="MPV-1", status="waiting", phase="waiting"
        )

    async def touch() -> None:
        llamadas.append(1)

    async def sleep(_: float) -> None:
        return None

    eventos = waiting_room.sse_events(
        fetch,
        requested_id=cid,
        poll_seconds=0.0,
        heartbeat_seconds=100.0,
        max_seconds=10.0,
        clock=lambda: next(reloj),
        sleep=sleep,
        touch_presence=touch,
    )
    async for _ in eventos:
        pass
    assert len(llamadas) >= 1


async def test_sse_sigue_funcionando_si_la_presencia_falla() -> None:
    """La presencia nunca tumba la sala de espera: el paciente está mirando esa pantalla."""
    from src.schemas.consultation import WaitingRoomResponse
    from src.services import waiting_room

    cid = uuid.uuid4()
    reloj = iter([0.0, 0.0, 999.0, 999.0])

    async def fetch() -> WaitingRoomResponse:
        return WaitingRoomResponse(
            consultation_id=cid, code="MPV-1", status="waiting", phase="waiting"
        )

    async def touch() -> None:
        raise RuntimeError("base caída")

    async def sleep(_: float) -> None:
        return None

    salida = [
        chunk
        async for chunk in waiting_room.sse_events(
            fetch,
            requested_id=cid,
            poll_seconds=0.0,
            heartbeat_seconds=100.0,
            max_seconds=10.0,
            clock=lambda: next(reloj),
            sleep=sleep,
            touch_presence=touch,
        )
    ]
    assert any("event: status" in chunk for chunk in salida)


# =====================================================================
# R3 — Diferir el aviso al paciente en línea
# =====================================================================


async def test_diferido_paciente_en_linea_no_recibe_correo(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    correos_capturados: list[dict],
) -> None:
    """CA3.1: con presencia fresca el mensaje se entrega (201) y el correo se aplaza."""
    doc = await add_doctor(db_session)
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    presence.touch_patient_presence(consultation)
    await db_session.flush()

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Tómese el medicamento cada 8 horas."},
    )
    assert resp.status_code == 201
    assert correos_capturados == []
    await db_session.refresh(consultation)
    assert consultation.patient_notice_pending_at is not None
    assert consultation.patient_notice_sent_at is None


async def test_diferido_paciente_offline_recibe_el_correo_ya(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    correos_capturados: list[dict],
) -> None:
    """CA3.4: sin presencia, nada cambia respecto a hoy — el correo sale en el acto."""
    doc = await add_doctor(db_session)
    doc.full_name = "Ana Pérez"
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    _offline(consultation)
    await db_session.flush()

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Tómese el medicamento cada 8 horas."},
    )
    assert resp.status_code == 201
    assert len(correos_capturados) == 1
    assert correos_capturados[0]["to"] == "paciente@example.com"
    assert correos_capturados[0]["subject"] == "Ana Pérez te escribió por tu caso"
    assert "medicamento" not in correos_capturados[0]["text"]
    await db_session.refresh(consultation)
    assert consultation.patient_notice_pending_at is None
    assert consultation.patient_notice_sent_at is not None


async def test_leer_cancela_el_aviso_diferido(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    correos_capturados: list[dict],
) -> None:
    """CA3.2: el paciente lee dentro de la ventana ⇒ el correo no sale NUNCA. Es el objetivo
    entero del diferido."""
    doc = await add_doctor(db_session)
    consultation, _, token = await _caso(client, db_session, doctor=doc)
    presence.touch_patient_presence(consultation)
    await db_session.flush()

    await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Tómese el medicamento."},
    )
    await db_session.refresh(consultation)
    assert consultation.patient_notice_pending_at is not None

    leer = await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages/read",
        headers={"X-Consultation-Token": token},
    )
    assert leer.status_code == 200
    await db_session.refresh(consultation)
    assert consultation.patient_notice_pending_at is None

    assert await notifications.send_pending_message_notices(db_session) == 0
    assert correos_capturados == []


async def test_barrido_envia_lo_vencido_y_sin_leer(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    correos_capturados: list[dict],
) -> None:
    """CA3.3: no lo leyó ⇒ pasada la espera el barrido sí le escribe. Y una segunda pasada no
    manda nada: el pendiente se limpia antes del intento."""
    doc = await add_doctor(db_session)
    doc.full_name = "Ana Pérez"
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    presence.touch_patient_presence(consultation)
    await db_session.flush()

    await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Tómese el medicamento."},
    )
    await db_session.refresh(consultation)

    # Aún no vencido: el barrido no lo toca.
    assert await notifications.send_pending_message_notices(db_session) == 0
    await db_session.refresh(consultation)
    assert consultation.patient_notice_pending_at is not None

    consultation.patient_notice_pending_at = datetime.now(UTC) - timedelta(
        seconds=settings.MESSAGING_MAIL_ONLINE_DELAY_SECONDS + 1
    )
    await db_session.flush()

    assert await notifications.send_pending_message_notices(db_session) == 1
    assert len(correos_capturados) == 1
    assert correos_capturados[0]["subject"] == "Ana Pérez te escribió por tu caso"
    assert "medicamento" not in correos_capturados[0]["text"]

    # Segunda pasada: nada que mandar (dos pasadas solapadas del cron no duplican).
    assert await notifications.send_pending_message_notices(db_session) == 0
    assert len(correos_capturados) == 1


async def test_barrido_descarta_lo_que_ya_se_leyo(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    correos_capturados: list[dict],
) -> None:
    """CA3.3: un pendiente vencido cuyos mensajes ya están leídos se descarta sin enviar."""
    doc = await add_doctor(db_session)
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    presence.touch_patient_presence(consultation)
    await db_session.flush()

    await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Tómese el medicamento."},
    )
    await db_session.refresh(consultation)
    # Vencido pero sin nada sin leer: se descarta y queda limpio.
    consultation.patient_notice_pending_at = datetime.now(UTC) - timedelta(
        seconds=settings.MESSAGING_MAIL_ONLINE_DELAY_SECONDS + 1
    )
    from sqlalchemy import update

    from src.models.clinical import Message

    await db_session.execute(
        update(Message)
        .where(Message.consultation_id == consultation.id)
        .values(read_at=datetime.now(UTC))
    )
    await db_session.flush()

    assert await notifications.send_pending_message_notices(db_session) == 0
    assert correos_capturados == []
    await db_session.refresh(consultation)
    assert consultation.patient_notice_pending_at is None


async def test_el_aviso_al_medico_nunca_se_difiere(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    correos_capturados: list[dict],
) -> None:
    """CA3.5: la presencia del médico no se registra (asimetría deliberada), así que su aviso
    sale siempre en el acto — incluso con el paciente en línea."""
    doc = await add_doctor(db_session)
    doc.email = f"doc-{uuid.uuid4().hex[:10]}@example.com"
    consultation, _, token = await _caso(client, db_session, doctor=doc)
    presence.touch_patient_presence(consultation)
    await db_session.flush()

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Doctora, me siento peor."},
    )
    assert resp.status_code == 201
    assert len(correos_capturados) == 1
    assert correos_capturados[0]["to"] == doc.email
    assert correos_capturados[0]["subject"] == "Tu paciente te escribió"


async def test_endpoint_del_barrido_exige_permiso(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """T16: el endpoint del cron está detrás de `queue.manage`; un médico normal no lo corre."""
    ruta = f"{PREFIX}/consultations/messages/send-pending-notices"

    doc = await add_doctor(db_session)
    denegado = await anon_client.post(ruta, headers=auth_headers(doc.id))
    assert denegado.status_code == 403

    permitido = await client.post(ruta)
    assert permitido.status_code == 200, permitido.text
    cuerpo = permitido.json()
    assert cuerpo["sent"] == 0
    assert cuerpo["delay_seconds"] == settings.MESSAGING_MAIL_ONLINE_DELAY_SECONDS


# =====================================================================
# R4 — Anti-ráfaga en base de datos, una sola regla
# =====================================================================


async def test_debounce_envia_frena_expira_y_se_levanta_al_leer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA4.2/CA4.3: los cuatro casos de la única regla, sin tocar memoria de proceso."""
    doc = await add_doctor(db_session)
    doc.email = f"doc-{uuid.uuid4().hex[:10]}@example.com"
    consultation, _, _ = await _caso(client, db_session, doctor=doc)

    # 1. Primer aviso: la columna está en null, no hay nada de lo que proteger.
    assert (
        await notifications.should_debounce_mail(db_session, consultation, "doctor_to_patient")
        is False
    )

    # Un mensaje sin leer + aviso recién salido ⇒ frena.
    from src.models.clinical import Message

    db_session.add(
        Message(
            id=uuid.uuid4(),
            consultation_id=consultation.id,
            sender_role="doctor",
            sender_user_id=doc.id,
            direction="doctor_to_patient",
            channel="web",
            body="Mensaje de prueba",
            sent_at=datetime.now(UTC),
        )
    )
    consultation.patient_notice_sent_at = datetime.now(UTC)
    await db_session.flush()
    assert (
        await notifications.should_debounce_mail(db_session, consultation, "doctor_to_patient")
        is True
    )

    # 3. Pasada la ventana, deja pasar aunque siga sin leerse.
    consultation.patient_notice_sent_at = datetime.now(UTC) - timedelta(
        minutes=settings.MESSAGING_MAIL_DEBOUNCE_MINUTES + 1
    )
    await db_session.flush()
    assert (
        await notifications.should_debounce_mail(db_session, consultation, "doctor_to_patient")
        is False
    )

    # 4. Tras leer, el freno se levanta: un mensaje nuevo es información nueva.
    consultation.patient_notice_sent_at = datetime.now(UTC)
    await db_session.flush()
    notifications.clear_notice_state(consultation, "patient")
    assert consultation.patient_notice_sent_at is None
    assert (
        await notifications.should_debounce_mail(db_session, consultation, "doctor_to_patient")
        is False
    )


async def test_debounce_es_independiente_por_destinatario(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """CA4.1: los dos lados del hilo tienen su propia columna. Que el médico tenga un aviso
    reciente no puede callar el del paciente."""
    doc = await add_doctor(db_session)
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    consultation.doctor_notice_sent_at = datetime.now(UTC)
    await db_session.flush()

    assert (
        await notifications.should_debounce_mail(db_session, consultation, "doctor_to_patient")
        is False
    )

    notifications.clear_notice_state(consultation, "doctor")
    assert consultation.doctor_notice_sent_at is None


async def test_debounce_tolera_un_timestamp_sin_zona(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Comparar naive con aware lanza `TypeError` justo en el camino que decide si sale un
    correo. La columna es `timestamptz`, pero un doble o una fila vieja pueden traer un naive."""
    doc = await add_doctor(db_session)
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    consultation.patient_notice_sent_at = datetime.now(UTC).replace(tzinfo=None)
    assert (
        await notifications.should_debounce_mail(db_session, consultation, "doctor_to_patient")
        is False
    )


# =====================================================================
# Caminos de "no hay a quién escribir" (ninguno es un fallo)
# =====================================================================


async def test_args_consulta_inexistente_no_revienta(db_session: AsyncSession) -> None:
    assert (
        await notifications.message_received_mail_args(
            db_session, uuid.uuid4(), uuid.uuid4(), "doctor_to_patient"
        )
        is None
    )


async def test_args_sin_medico_asignado_no_avisa(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un caso en cola sin tomar no tiene a quién avisar del mensaje del paciente."""
    doc = await add_doctor(db_session)
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    consultation.assigned_doctor_id = None
    await db_session.flush()
    assert (
        await notifications.message_received_mail_args(
            db_session, consultation.id, uuid.uuid4(), "patient_to_doctor"
        )
        is None
    )


async def test_args_direccion_desconocida_no_avisa(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`direction='system'` (el aviso de videollamada del hilo) no genera correo a nadie."""
    doc = await add_doctor(db_session)
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    assert (
        await notifications.message_received_mail_args(
            db_session, consultation.id, uuid.uuid4(), "system"
        )
        is None
    )


async def test_barrido_limpia_el_pendiente_de_un_paciente_sin_correo(
    client: AsyncClient, db_session: AsyncSession, correos_capturados: list[dict]
) -> None:
    """Si el correo del paciente desapareció entre el diferido y el barrido, el pendiente se
    limpia igual: dejarlo puesto lo haría reaparecer en cada pasada del cron para siempre."""
    doc = await add_doctor(db_session)
    consultation, patient, _ = await _caso(client, db_session, doctor=doc)
    consultation.patient_notice_pending_at = datetime.now(UTC) - timedelta(
        seconds=settings.MESSAGING_MAIL_ONLINE_DELAY_SECONDS + 1
    )
    patient.email = None
    await db_session.flush()

    assert await notifications.send_pending_message_notices(db_session) == 0
    assert correos_capturados == []
    await db_session.refresh(consultation)
    assert consultation.patient_notice_pending_at is None


async def test_persist_presence_con_consulta_inexistente_no_revienta(
    db_session: AsyncSession,
) -> None:
    """El stream ya tiene su propio camino de `gone`; la presencia no es donde descubrirlo."""
    assert await presence.persist_patient_presence(db_session, uuid.uuid4()) is False


async def test_persist_presence_respeta_el_throttle(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Dos ciclos seguidos del stream (4 s de separación) no son dos UPDATE."""
    doc = await add_doctor(db_session)
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    _offline(consultation)
    await db_session.flush()

    assert await presence.persist_patient_presence(db_session, consultation.id) is True
    assert await presence.persist_patient_presence(db_session, consultation.id) is False


async def test_diferido_no_se_posterga_con_cada_mensaje_nuevo(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    correos_capturados: list[dict],
) -> None:
    """El reloj del diferido lo fija el PRIMER mensaje sin entregar, no el último.

    Si se reescribiera en cada mensaje, un médico que manda varias frases seguidas a un paciente
    con la pestaña abierta pero sin leer empujaría el aviso hacia adelante para siempre.
    """
    doc = await add_doctor(db_session)
    consultation, _, _ = await _caso(client, db_session, doctor=doc)
    presence.touch_patient_presence(consultation)
    await db_session.flush()

    for cuerpo in ("Primera frase.", "Segunda frase."):
        resp = await anon_client.post(
            f"{PREFIX}/consultations/{consultation.id}/messages",
            headers=auth_headers(doc.id),
            json={"body": cuerpo},
        )
        assert resp.status_code == 201
    await db_session.refresh(consultation)
    primer_reloj = consultation.patient_notice_pending_at
    assert primer_reloj is not None

    # Lo vencemos y mandamos una tercera: el reloj NO se reinicia, así que el barrido ya puede.
    consultation.patient_notice_pending_at = datetime.now(UTC) - timedelta(
        seconds=settings.MESSAGING_MAIL_ONLINE_DELAY_SECONDS + 1
    )
    await db_session.flush()
    vencido = consultation.patient_notice_pending_at

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Tercera frase."},
    )
    assert resp.status_code == 201
    await db_session.refresh(consultation)
    assert consultation.patient_notice_pending_at == vencido  # intacto
    assert await notifications.send_pending_message_notices(db_session) == 1
    assert len(correos_capturados) == 1


# =====================================================================
# Recorrido completo del paciente (las tres preguntas del 2026-10-17)
# =====================================================================


async def test_recorrido_paciente_desconectado_registro_claim_y_chat(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    correos_capturados: list[dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El recorrido entero de un paciente que NO está en la pantalla, en orden:
    registro → un médico toma el caso → el médico le escribe por el chat.

    Tres hechos distintos, tres correos. Existe como prueba porque es la pregunta que de verdad
    se hace: no "¿funciona el módulo?", sino "¿qué llega a la bandeja de este paciente?".
    """
    enviados_alta: list[dict] = []

    async def _fake_alta(to_email, subject, text, html=None, category=None, **_):
        enviados_alta.append({"to": to_email, "subject": subject, "text": text})
        return True

    monkeypatch.setattr(registration_mail, "send_mail", _fake_alta)

    doc = await add_doctor(db_session, specialty="Cardiología")
    doc.full_name = "Ana Pérez"

    # 1. Se registra (cola pública, con correo) -> confirmación al paciente.
    p_resp = await client.post(
        f"{PREFIX}/patients", json=valid_patient_payload(email="maria@ejemplo.org")
    )
    assert p_resp.status_code == 201, p_resp.text
    patient_id = p_resp.json()["id"]
    c_resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "specialty_id": str(doc.specialty_id),
            "chief_complaint": "Motivo clínico inicial",
        },
    )
    assert c_resp.status_code == 201, c_resp.text
    consultation = await db_session.get(Consultation, uuid.UUID(c_resp.json()["id"]))
    assert consultation is not None
    al_paciente = [c for c in enviados_alta if c["to"] == "maria@ejemplo.org"]
    assert len(al_paciente) == 1
    assert "registrado" in al_paciente[0]["subject"].lower()

    # El paciente cierra la pestaña: ni memoria ni columna.
    _offline(consultation)
    await db_session.flush()

    # 2. Un médico toma el caso -> aviso de videoconsulta CON el nombre del médico.
    claim = await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/claim", headers=auth_headers(doc.id)
    )
    assert claim.status_code == 200, claim.text
    await db_session.refresh(consultation)
    assert consultation.video_room_url  # la sala se crea en el mismo UPDATE del claim
    assert len(correos_capturados) == 1
    assert correos_capturados[0]["to"] == "maria@ejemplo.org"
    assert correos_capturados[0]["subject"] == "Tu médico te está esperando en la videoconsulta"
    assert "Ana Pérez" in correos_capturados[0]["text"]

    # 3. El médico le escribe por el chat -> segundo aviso, con nombre y "revisó tu caso".
    msg = await anon_client.post(
        f"{PREFIX}/consultations/{consultation.id}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Buenas tardes, cuénteme cómo se siente."},
    )
    assert msg.status_code == 201, msg.text
    assert len(correos_capturados) == 2
    assert correos_capturados[1]["subject"] == "Ana Pérez te escribió por tu caso"
    assert "ha revisado tu caso" in correos_capturados[1]["text"]
    # Y sigue sin una palabra de lo que el médico escribió.
    assert "cómo se siente" not in correos_capturados[1]["text"]
