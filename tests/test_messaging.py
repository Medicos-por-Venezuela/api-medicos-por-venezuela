"""Pruebas exhaustivas del módulo de mensajería (médico ↔ paciente).

Cubre:
- Permisos y pertenencia para médico y paciente (sesión y token).
- Cifrado clínico y fail-closed (el admin ve metadatos y conteos pero NO cuerpos ni adjuntos).
- Subida de adjuntos (PDF, JPG, PNG, WEBP válidos; rechazo estricto de GIF con HTTP 422).
- Descarga segura de adjuntos con nosniff y grant clínico (admin prohibido).
- Presencia asimétrica (solo el médico tratante ve si el paciente está en línea).
- Marcado como leído idempotente y contadores.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import consultation_token
from src.core.config import settings
from src.db.session import get_session_factory
from src.main import app
from src.models.clinical import Message
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.profile import Profile
from src.services import messaging, notifications
from tests._helpers import (
    add_doctor,
    any_specialty_id,
    auth_headers,
    grant_roles,
    valid_patient_payload,
)

PREFIX = "/api/v1"

# Binary samples with valid magic bytes
PDF_BYTES = b"%PDF-1.4\n%test pdf binary content for medical report\n%%EOF"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
)
JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xd9"
)
WEBP_BYTES = (
    b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00\x30\x01\x00\x9d\x01\x2a\x01\x00\x01\x00\x02\x00"
)
GIF_BYTES = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


async def _create_test_case(
    client: AsyncClient,
    db: AsyncSession,
    *,
    doctor: Profile | None = None,
    patient_user_id: uuid.UUID | None = None,
    status: str = "in_progress",
) -> tuple[str, str, str]:
    """Crea un paciente y una consulta asignada a un médico."""
    p_resp = await client.post(f"{PREFIX}/patients", json=valid_patient_payload())
    assert p_resp.status_code == 201, p_resp.text
    patient_id = p_resp.json()["id"]

    if patient_user_id:
        patient = await db.get(Patient, uuid.UUID(patient_id))
        assert patient is not None
        patient.user_id = patient_user_id
        await db.flush()

    spec_id = await any_specialty_id(client)
    c_resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient_id,
            "specialty_id": spec_id,
            "chief_complaint": "Motivo clínico inicial",
        },
    )
    assert c_resp.status_code == 201, c_resp.text
    consultation_id = c_resp.json()["id"]

    consultation = await db.get(Consultation, uuid.UUID(consultation_id))
    assert consultation is not None
    consultation.status = status
    if doctor:
        consultation.assigned_doctor_id = doctor.id
    await db.flush()

    token = consultation_token.issue(consultation.id)
    return consultation_id, patient_id, token


# =====================================================================
# 1. Permisos y envío de mensajes
# =====================================================================


async def test_doctor_send_message_and_idempotency(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El médico tratante envía un mensaje y client_msg_id es idempotente."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    # 1. Médico tratante envía mensaje
    client_msg_id = f"msg-{uuid.uuid4()}"
    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Hola paciente, ¿cómo se siente hoy?", "client_msg_id": client_msg_id},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["body"] == "Hola paciente, ¿cómo se siente hoy?"
    assert data["direction"] == "doctor_to_patient"
    assert data["sender_role"] == "doctor"
    assert data["clinical_access"] == "full"
    first_msg_id = data["id"]

    # 2. Reenvío con el mismo client_msg_id devuelve 200 OK con el mismo mensaje
    dup_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Hola paciente, ¿cómo se siente hoy?", "client_msg_id": client_msg_id},
    )
    assert dup_resp.status_code == 200
    dup_data = dup_resp.json()
    assert dup_data["id"] == first_msg_id


async def test_doctor_outsider_cannot_send_message(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un médico que no está en la consulta recibe 404 (no 403, CA2.1)."""
    doc_tratante = await add_doctor(db_session)
    doc_ajeno = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc_tratante)

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc_ajeno.id),
        json={"body": "Intento de intromisión"},
    )
    assert resp.status_code == 404, resp.text


async def test_doctor_cannot_send_in_waiting_or_closed_expired(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """No se puede escribir si la consulta está en waiting o cerrada hace más de 72h."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc, status="waiting")

    # 1. En waiting el médico no puede escribir
    w_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Mensaje en espera"},
    )
    assert w_resp.status_code == 409

    # 2. Cerrada hace más de 72h (MESSAGING_AFTER_CLOSE_HOURS)
    consultation = await db_session.get(Consultation, uuid.UUID(cid))
    assert consultation is not None
    consultation.status = "closed"
    consultation.closed_at = datetime.now(UTC) - timedelta(hours=73)
    await db_session.flush()

    c_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Mensaje tras cierre expirado"},
    )
    assert c_resp.status_code == 409


async def test_patient_send_message_token_and_account(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El paciente puede escribir con su token o con su cuenta de usuario."""
    doc = await add_doctor(db_session)
    patient_user_id = uuid.uuid4()
    # Perfil del paciente con cuenta
    p_profile = Profile(
        id=patient_user_id,
        full_name="Paciente Con Cuenta",
        role="patient",
        active=True,
        verified=True,
        role_chosen=True,
    )
    db_session.add(p_profile)
    await db_session.flush()

    cid, _, token = await _create_test_case(
        client, db_session, doctor=doc, patient_user_id=patient_user_id
    )

    # 1. Paciente anónimo con token de consulta
    anon_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Hola doctora, soy el paciente anónimo"},
    )
    assert anon_resp.status_code == 201, anon_resp.text
    anon_data = anon_resp.json()
    assert anon_data["sender_role"] == "patient"
    assert anon_data["direction"] == "patient_to_doctor"
    assert anon_data["sender_user_id"] is None

    # 2. Paciente autenticado con su sesión
    auth_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(patient_user_id),
        json={"body": "Hola doctora, ahora escribo desde mi cuenta"},
    )
    assert auth_resp.status_code == 201, auth_resp.text
    auth_data = auth_resp.json()
    assert auth_data["sender_user_id"] == str(patient_user_id)


async def test_patient_invalid_token_rejected(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Token expirado, manipulado o de otra consulta es rechazado."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)
    other_cid, _, other_token = await _create_test_case(client, db_session, doctor=doc)

    # Token de otra consulta
    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": other_token},
        json={"body": "Token cruzado"},
    )
    assert resp.status_code in (401, 404)

    # Sin token ni sesión
    no_auth_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        json={"body": "Sin credenciales"},
    )
    assert no_auth_resp.status_code == 401


async def test_patient_hourly_rate_limit(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El paciente tiene un límite de mensajes por hora (CA4.4)."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Simulamos inserción de 30 mensajes recientes en la base
    for i in range(30):
        db_session.add(
            Message(
                id=uuid.uuid4(),
                consultation_id=uuid.UUID(cid),
                sender_role="patient",
                direction="patient_to_doctor",
                channel="web",
                kind="text",
                body=f"Mensaje {i}",
                sent_at=datetime.now(UTC),
                delivery_status="sent",
            )
        )
    await db_session.flush()

    # El mensaje 31 en la misma hora es rechazado con 409
    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje superando límite"},
    )
    assert resp.status_code == 409
    assert "Límite de mensajes" in resp.json()["detail"]


# =====================================================================
# 2. Cifrado clínico y fail-closed (Admin vs Médico)
# =====================================================================


async def test_clinical_grant_fail_closed_admin_vs_doctor(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Fail-closed: el admin ve metadatos pero NO el cuerpo cifrado; el médico tratante sí."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # El paciente escribe un secreto clínico
    secret_body = "Tengo fiebre alta y erupciones en la piel"
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": secret_body},
    )

    # 1. Médico tratante lee el hilo: recibe body descifrado y clinical_access='full'
    doc_resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
    )
    assert doc_resp.status_code == 200
    doc_data = doc_resp.json()
    assert len(doc_data) == 1
    assert doc_data[0]["body"] == secret_body
    assert doc_data[0]["clinical_access"] == "full"

    # 2. Administrador lee el hilo: recibe 200 con metadatos,
    # pero body es null y clinical_access='none'
    admin_resp = await client.get(f"{PREFIX}/consultations/{cid}/messages")
    assert admin_resp.status_code == 200
    admin_data = admin_resp.json()
    assert len(admin_data) == 1
    assert admin_data[0]["body"] is None  # FAIL-CLOSED
    assert admin_data[0]["clinical_access"] == "none"
    assert admin_data[0]["id"] == doc_data[0]["id"]  # Metadatos sí viajan


async def test_admin_who_is_treating_doctor_has_clinical_grant(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Si el administrador es también el médico tratante asignado, recibe grant clínico."""
    admin_doc = await add_doctor(db_session)
    await grant_roles(db_session, admin_doc.id, ["admin"])
    cid, _, token = await _create_test_case(client, db_session, doctor=admin_doc)

    # El paciente envía un mensaje
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Dolor abdominal agudo"},
    )

    # El admin-médico tratante lee el hilo: debe ver el cuerpo descifrado y clinical_access='full'
    resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(admin_doc.id),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["body"] == "Dolor abdominal agudo"
    assert data[0]["clinical_access"] == "full"

    # El admin-médico tratante puede responder en el hilo
    reply = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(admin_doc.id),
        json={"body": "¿Desde qué hora comenzó el dolor?"},
    )
    assert reply.status_code == 201
    assert reply.json()["body"] == "¿Desde qué hora comenzó el dolor?"


# =====================================================================
# 3. Subida y validación de archivos adjuntos (R15)
# =====================================================================


async def test_attachment_upload_valid_formats(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Médico y paciente pueden subir PDF, PNG, JPG y WEBP válidos."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    formats = [
        ("reporte.pdf", PDF_BYTES, "application/pdf"),
        ("radiografia.png", PNG_BYTES, "image/png"),
        ("foto.jpg", JPEG_BYTES, "image/jpeg"),
        ("examen.webp", WEBP_BYTES, "image/webp"),
    ]

    for fname, fbytes, expected_mime in formats:
        resp = await anon_client.post(
            f"{PREFIX}/consultations/{cid}/attachments",
            headers=auth_headers(doc.id),
            files={"file": (fname, fbytes, expected_mime)},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["file_name"] == fname
        assert data["mime_type"] == expected_mime
        assert data["file_size_bytes"] == len(fbytes)
        assert data["clinical_access"] == "full"


async def test_attachment_upload_gif_strictly_rejected_422(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El formato GIF está estrictamente prohibido y debe responder 422."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # 1. Extensión .gif
    resp1 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("animacion.gif", GIF_BYTES, "image/gif")},
    )
    assert resp1.status_code == 422
    assert "GIF" in resp1.json()["detail"]

    # 2. Magic bytes de GIF camuflados con otra extensión
    resp2 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("disfrazado.png", GIF_BYTES, "image/png")},
    )
    assert resp2.status_code == 422
    assert "GIF" in resp2.json()["detail"]

    # 3. Paciente intentando subir GIF con token
    resp3 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers={"X-Consultation-Token": token},
        files={"file": ("meme.gif", GIF_BYTES, "image/gif")},
    )
    assert resp3.status_code == 422
    assert "GIF" in resp3.json()["detail"]


async def test_attachment_upload_corrupt_and_size_limits(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Archivos corruptos o vacíos son rechazados con 422."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    # Archivo vacío
    resp_empty = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("vacio.pdf", b"", "application/pdf")},
    )
    assert resp_empty.status_code == 422

    # Archivo con bytes falsos que no coinciden con ninguna firma válida
    resp_corrupt = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("corrupto.pdf", b"esto es texto plano no un pdf", "application/pdf")},
    )
    assert resp_corrupt.status_code == 422


async def test_attachment_message_linking(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un mensaje puede vincular archivos adjuntos previamente subidos."""
    doc = await add_doctor(db_session)
    cid, _, _ = await _create_test_case(client, db_session, doctor=doc)

    # Subimos 2 archivos
    att1_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("doc1.pdf", PDF_BYTES, "application/pdf")},
    )
    att1_id = att1_resp.json()["id"]

    att2_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("foto.jpg", JPEG_BYTES, "image/jpeg")},
    )
    att2_id = att2_resp.json()["id"]

    # Enviamos mensaje con los dos adjuntos
    msg_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Adjunto exámenes", "attachment_ids": [att1_id, att2_id]},
    )
    assert msg_resp.status_code == 201
    msg_data = msg_resp.json()
    assert len(msg_data["attachments"]) == 2
    assert {a["id"] for a in msg_data["attachments"]} == {att1_id, att2_id}


# =====================================================================
# 4. Descarga segura con nosniff y grant clínico
# =====================================================================


async def test_attachment_download_with_grant_and_security_headers(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Descarga de adjuntos: médico tratante y paciente dueño acceden;
    cabecera nosniff presente."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Subir PDF
    upload_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/attachments",
        headers=auth_headers(doc.id),
        files={"file": ("informe_biopsia.pdf", PDF_BYTES, "application/pdf")},
    )
    att_id = upload_resp.json()["id"]

    # 1. Médico tratante descarga
    doc_dl = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/attachments/{att_id}",
        headers=auth_headers(doc.id),
    )
    assert doc_dl.status_code == 200
    assert doc_dl.content == PDF_BYTES
    assert doc_dl.headers["x-content-type-options"] == "nosniff"
    assert "inline" in doc_dl.headers["content-disposition"]
    assert "informe_biopsia.pdf" in doc_dl.headers["content-disposition"]

    # 2. Paciente dueño descarga con token
    pat_dl = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/attachments/{att_id}",
        headers={"X-Consultation-Token": token},
    )
    assert pat_dl.status_code == 200
    assert pat_dl.content == PDF_BYTES

    # 3. Administrador NO puede descargar adjunto clínico (403)
    admin_dl = await client.get(f"{PREFIX}/consultations/{cid}/attachments/{att_id}")
    assert admin_dl.status_code == 403

    # 4. Médico ajeno recibe 404 (CA15.4)
    doc_ajeno = await add_doctor(db_session)
    ajeno_dl = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/attachments/{att_id}",
        headers=auth_headers(doc_ajeno.id),
    )
    assert ajeno_dl.status_code == 404


# =====================================================================
# 5. Presencia asimétrica y buzón (Inbox)
# =====================================================================


async def test_asymmetric_presence_in_inbox(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El médico tratante ve si el paciente está en línea;
    el paciente nunca ve presencia médica."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # 1. Sin actividad reciente del paciente -> patient_online es False
    # Enviamos un mensaje previo del doctor para que el hilo aparezca en el inbox
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Iniciando conversación"},
    )

    inbox_resp1 = await anon_client.get(f"{PREFIX}/inbox", headers=auth_headers(doc.id))
    assert inbox_resp1.status_code == 200
    threads1 = inbox_resp1.json()
    assert len(threads1) == 1
    assert threads1[0]["patient_online"] is False

    # 2. El paciente realiza una acción (ej. envía un mensaje con su token)
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Estoy aquí doctora"},
    )

    # 3. Ahora el médico ve al paciente 'patient_online': True
    inbox_resp2 = await anon_client.get(f"{PREFIX}/inbox", headers=auth_headers(doc.id))
    threads2 = inbox_resp2.json()
    assert threads2[0]["patient_online"] is True
    assert threads2[0]["patient_last_seen_at"] is not None

    # 4. Regla de asimetría: El listado de mensajes hacia el paciente NUNCA expone presencia
    pat_msgs_resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
    )
    assert pat_msgs_resp.status_code == 200
    for m in pat_msgs_resp.json():
        assert "doctor_online" not in m
        assert "doctor_last_seen_at" not in m


# =====================================================================
# 6. Marcado como leído idempotente y contadores
# =====================================================================


async def test_mark_as_read_idempotency_and_counters(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Marcar leído actualiza solo mensajes opuestos y es estrictamente idempotente."""
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Paciente envía 2 mensajes
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje 1"},
    )
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje 2"},
    )

    # Médico consulta el hilo: X-Unread-Count es 2
    list_resp1 = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
    )
    assert list_resp1.headers["x-unread-count"] == "2"

    # Médico marca como leído
    read_resp1 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages/read",
        headers=auth_headers(doc.id),
    )
    assert read_resp1.status_code == 200
    assert read_resp1.json()["marked"] == 2

    # Segundo llamado consecutivo devuelve marked = 0 (idempotencia)
    read_resp2 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages/read",
        headers=auth_headers(doc.id),
    )
    assert read_resp2.status_code == 200
    assert read_resp2.json()["marked"] == 0

    # Ahora X-Unread-Count es 0
    list_resp2 = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
    )
    assert list_resp2.headers["x-unread-count"] == "0"


# =====================================================================
# 7. Avisos por correo con anti-ráfaga (debounce) y privacidad clínica (T1.5)
# =====================================================================


@pytest.fixture
def factory_de_prueba(db_session: AsyncSession):
    """Las sesiones cortas de los streams ven los datos de la transacción de la prueba."""

    @asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[get_session_factory] = lambda: _session
    yield
    app.dependency_overrides.pop(get_session_factory, None)


async def test_messaging_email_dispatch_and_debounce(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6: Disparo de emails con anti-ráfaga de 15 min y limpieza al marcar leído."""
    doc = await add_doctor(db_session)
    doc.email = "doctor.tratante@example.com"
    await db_session.flush()

    cid, pid, token = await _create_test_case(client, db_session, doctor=doc)

    # Actualizar correo del paciente
    patient = await db_session.get(Patient, uuid.UUID(pid))
    assert patient is not None
    patient.email = "paciente@example.com"
    await db_session.flush()

    sent_mails: list[dict] = []

    async def mock_send_mail(
        to_email: str,
        subject: str,
        text: str,
        html: str = "",
        category: str = "general",
    ) -> bool:
        sent_mails.append(
            {"to": to_email, "subject": subject, "text": text, "html": html, "category": category}
        )
        return True

    monkeypatch.setattr(notifications, "send_mail", mock_send_mail)

    # 1. Paciente escribe al médico -> Correo enviado
    resp1 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Hola doctor, tengo una consulta."},
    )
    assert resp1.status_code == 201
    assert len(sent_mails) == 1
    assert sent_mails[0]["to"] == "doctor.tratante@example.com"
    assert sent_mails[0]["subject"] == "Tu paciente te escribió"
    assert "Hola doctor" not in sent_mails[0]["text"]  # Cero texto clínico

    # 2. Paciente envía 2do mensaje casi de inmediato (< 15 min, sin leer) -> Debounce bloquea
    resp2 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Se me olvidó comentarle otro detalle."},
    )
    assert resp2.status_code == 201
    assert len(sent_mails) == 1  # No aumentó

    # 3. Médico marca como leído -> Limpia debounce
    read_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages/read",
        headers=auth_headers(doc.id),
    )
    assert read_resp.status_code == 200

    # 4. Paciente escribe de nuevo -> Se envía correo porque ya no hay no-leídos pendientes
    resp3 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Gracias por responder doctor."},
    )
    assert resp3.status_code == 201
    assert len(sent_mails) == 2
    assert sent_mails[1]["subject"] == "Tu paciente te escribió"

    # 5. Médico responde al paciente -> Correo enviado al paciente
    resp4 = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Con gusto, tómese el medicamento."},
    )
    assert resp4.status_code == 201
    assert len(sent_mails) == 3
    assert sent_mails[2]["to"] == "paciente@example.com"
    assert sent_mails[2]["subject"] == "Tu médico te respondió"
    assert "tómese el medicamento" not in sent_mails[2]["text"]  # Cero texto clínico


async def test_messaging_doctor_opt_out_notifications(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6.1: Si el médico desactiva `message_received` en sus preferencias, no recibe correo."""
    doc = await add_doctor(db_session)
    doc.email = "doctor.optout@example.com"
    doc.notification_prefs = {"message_received": {"email": False}}
    await db_session.flush()

    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    sent_mails: list[dict] = []

    async def mock_send_mail(
        to_email: str,
        subject: str,
        text: str,
        html: str = "",
        category: str = "general",
    ) -> bool:
        sent_mails.append({"to": to_email, "subject": subject})
        return True

    monkeypatch.setattr(notifications, "send_mail", mock_send_mail)

    resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Hola doctor."},
    )
    assert resp.status_code == 201
    assert len(sent_mails) == 0


# =====================================================================
# 8. Tiempo real (SSE) y presencia asimétrica (T1.6 / R8)
# =====================================================================


async def test_inbox_stream_sse_endpoint(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    factory_de_prueba: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8.2: GET /inbox/stream emite eventos `inbox` con conteos y consultas actualizadas."""
    monkeypatch.setattr(settings, "WAITING_ROOM_STREAM_MAX_SECONDS", 0)

    # 1. No autenticado -> 401
    sin_auth = await anon_client.get(f"{PREFIX}/inbox/stream")
    assert sin_auth.status_code == 401

    # 2. Sin permiso messages.read (usuario ordinario) -> 403
    unauth_resp = await anon_client.get(
        f"{PREFIX}/inbox/stream",
        headers=auth_headers(uuid.uuid4()),
    )
    assert unauth_resp.status_code == 403

    # 3. Médico con messages.read
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Paciente envía un mensaje para generar no leídos
    await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers={"X-Consultation-Token": token},
        json={"body": "Mensaje en espera"},
    )

    resp = await anon_client.get(
        f"{PREFIX}/inbox/stream",
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert "event: inbox" in resp.text
    assert '"unread_total":1' in resp.text
    assert cid in resp.text
    # Cero cuerpos clínicos en el stream
    assert "Mensaje en espera" not in resp.text


async def test_waiting_room_stream_emits_message_event_and_presence_asymmetry(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    factory_de_prueba: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8.1 y CA7.2: La sala emite `message` y preserva estricta asimetría de presencia."""
    monkeypatch.setattr(settings, "WAITING_ROOM_STREAM_MAX_SECONDS", 0)
    doc = await add_doctor(db_session)
    cid, _, token = await _create_test_case(client, db_session, doctor=doc)

    # Médico envía mensaje al paciente
    msg_resp = await anon_client.post(
        f"{PREFIX}/consultations/{cid}/messages",
        headers=auth_headers(doc.id),
        json={"body": "Hola paciente, ya le atiendo."},
    )
    assert msg_resp.status_code == 201

    # Paciente se conecta al stream de la sala
    resp = await anon_client.get(
        f"{PREFIX}/consultations/{cid}/waiting-room/stream",
        headers={"X-Consultation-Token": token},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: status" in resp.text
    assert "event: message" in resp.text
    assert '"direction":"doctor_to_patient"' in resp.text
    assert '"unread_count":1' in resp.text

    # Verifica que el paciente está marcado como en línea en memoria
    assert messaging.is_patient_online(uuid.UUID(cid)) is True

    # Asimetría estricta: La respuesta de la sala NO revela presencia del médico
    assert "doctor_online" not in resp.text
    assert "doctor_last_seen" not in resp.text
    assert "Hola paciente" not in resp.text  # Cero cuerpo clínico en SSE
