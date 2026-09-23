"""Tests del módulo Agenda: agendar seguimiento (padre→hija), firma al cerrar, agenda, cadena."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.profile import Profile
from src.services import notifications
from tests._helpers import GENERAL, add_doctor, any_specialty_id, auth_headers, make_profile

PREFIX = "/api/v1"


async def _open_consultation(client: AsyncClient, doctor_id) -> str:
    """Crea paciente + consulta y la TOMA el médico (queda in_progress, asignada)."""
    p = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Pac Agenda",
            "phone_whatsapp": "+58412555222",
            "emergency_phone": "+58414555222",
            "address_encrypted": "v1:dGVzdCBjaXBoZXJ0ZXh0",
            "affected_zone": "Caracas",
            "consent": True,
        },
    )
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={
                "patient_id": p.json()["id"],
                "chief_complaint": "Dolor",
                "specialty_id": await any_specialty_id(client),
            },
        )
    ).json()["id"]
    r = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(doctor_id)
    )
    assert r.status_code == 200, r.text
    return cid


async def test_schedule_follow_up_closes_parent_and_creates_child(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    parent_cid = await _open_consultation(client, doc.id)

    when = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    resp = await client.post(
        f"{PREFIX}/consultations/{parent_cid}/schedule-follow-up",
        json={
            "scheduled_at": when,
            "closing_note": "seguimiento",
            "signature": "data:image/png;x",
        },
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 201, resp.text
    child = resp.json()
    assert child["status"] == "scheduled"
    assert child["parent_consultation_id"] == parent_cid
    assert child["scheduled_at"] is not None
    assert child["id"] != parent_cid

    parent = await client.get(f"{PREFIX}/consultations/{parent_cid}", headers=auth_headers(doc.id))
    assert parent.json()["status"] == "closed"


async def test_schedule_follow_up_rejects_past_date(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid = await _open_consultation(client, doc.id)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/schedule-follow-up",
        json={"scheduled_at": past},
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 422


async def test_agenda_lists_doctor_scheduled(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid = await _open_consultation(client, doc.id)
    when = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    child = (
        await client.post(
            f"{PREFIX}/consultations/{cid}/schedule-follow-up",
            json={"scheduled_at": when},
            headers=auth_headers(doc.id),
        )
    ).json()

    agenda = await client.get(f"{PREFIX}/consultations/agenda", headers=auth_headers(doc.id))
    assert agenda.status_code == 200, agenda.text
    items = agenda.json()
    assert child["id"] in {c["id"] for c in items}
    assert all(c["status"] == "scheduled" for c in items)


async def test_chain_returns_parent_and_child(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    parent_cid = await _open_consultation(client, doc.id)
    when = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    child = (
        await client.post(
            f"{PREFIX}/consultations/{parent_cid}/schedule-follow-up",
            json={"scheduled_at": when},
            headers=auth_headers(doc.id),
        )
    ).json()

    chain = await client.get(
        f"{PREFIX}/consultations/{child['id']}/chain", headers=auth_headers(doc.id)
    )
    assert chain.status_code == 200, chain.text
    ids = [c["id"] for c in chain.json()]
    assert parent_cid in ids and child["id"] in ids
    assert ids[0] == parent_cid  # la raíz (padre) va primero


async def test_refer_hands_off_parent_and_schedules_for_specialist(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    specialist = await add_doctor(db_session, role="specialist")
    parent_cid = await _open_consultation(client, doc.id)

    when = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    resp = await client.post(
        f"{PREFIX}/consultations/{parent_cid}/refer",
        json={
            "invited_doctor_id": str(specialist.id),
            "scheduled_at": when,
            "reason": "Requiere cardiología",
            "signature": "data:image/png;x",
        },
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 201, resp.text
    child = resp.json()
    assert child["status"] == "scheduled"
    assert child["parent_consultation_id"] == parent_cid
    assert child["assigned_doctor_id"] == str(specialist.id)
    # La hija ya es del especialista: quien refiere la recibe sin contenido clínico...
    assert child["internal_note"] is None
    assert child["clinical_access"] == "none"
    # ...y el especialista, como médico tratante, ve el motivo de la referencia.
    detalle = await client.get(
        f"{PREFIX}/consultations/{child['id']}", headers=auth_headers(specialist.id)
    )
    assert detalle.status_code == 200, detalle.text
    assert detalle.json()["internal_note"] == "Requiere cardiología"
    assert detalle.json()["chief_complaint"] == "Dolor"  # el motivo del padre, copiado cifrado

    # El padre queda derivado (ya no lo atiende el médico actual).
    parent = await client.get(f"{PREFIX}/consultations/{parent_cid}", headers=auth_headers(doc.id))
    assert parent.json()["status"] == "referred_to_specialist"

    # El especialista la ve en SU agenda con las notas previas (chain).
    agenda = await client.get(
        f"{PREFIX}/consultations/agenda", headers=auth_headers(specialist.id)
    )
    assert child["id"] in {c["id"] for c in agenda.json()}
    chain = await client.get(
        f"{PREFIX}/consultations/{child['id']}/chain", headers=auth_headers(specialist.id)
    )
    assert parent_cid in {c["id"] for c in chain.json()}
    assert all(c["clinical_access"] == "full" for c in chain.json())


async def test_el_correo_de_referencia_no_lleva_el_motivo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Decisión 2026-09-23: los correos no llevan texto clínico (solo fecha, código y enlace).
    El motivo de la referencia es nota del médico y un correo sale del sistema sin control."""
    doc = await add_doctor(db_session, specialty=GENERAL)
    specialist = await add_doctor(db_session, role="specialist")
    parent_cid = await _open_consultation(client, doc.id)
    capturado = AsyncMock(return_value=None)

    with patch("src.services.notifications.doctor_event_email_args", capturado):
        resp = await client.post(
            f"{PREFIX}/consultations/{parent_cid}/refer",
            json={
                "invited_doctor_id": str(specialist.id),
                "scheduled_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                "reason": "Sospecha de soplo cardíaco",
            },
            headers=auth_headers(doc.id),
        )

    assert resp.status_code == 201, resp.text
    texto = capturado.await_args.kwargs["text"]
    assert "soplo" not in texto
    assert "Motivo" not in texto
    assert resp.json()["code"] in texto


async def test_refer_rejects_self(client: AsyncClient, db_session: AsyncSession) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid = await _open_consultation(client, doc.id)
    when = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/refer",
        json={"invited_doctor_id": str(doc.id), "scheduled_at": when, "reason": "x"},
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 409, resp.text


async def _schedule_in(client, doc, cid, delta) -> dict:
    when = (datetime.now(UTC) + delta).isoformat()
    return (
        await client.post(
            f"{PREFIX}/consultations/{cid}/schedule-follow-up",
            json={"scheduled_at": when},
            headers=auth_headers(doc.id),
        )
    ).json()


async def test_send_due_reminders_marks_and_is_idempotent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, cid, timedelta(minutes=10))  # dentro de la ventana

    # El client por defecto es admin (queue.manage).
    r1 = await client.post(f"{PREFIX}/consultations/agenda/send-due-reminders")
    assert r1.status_code == 200, r1.text
    assert r1.json()["window_minutes"] == 30

    db_session.expire_all()
    row = await db_session.get(Consultation, uuid.UUID(child["id"]))
    assert row is not None and row.reminder_sent_at is not None
    first_marked = row.reminder_sent_at

    # 2ª corrida: ya tiene reminder_sent_at → no se re-marca (idempotente).
    r2 = await client.post(f"{PREFIX}/consultations/agenda/send-due-reminders")
    assert r2.status_code == 200
    db_session.expire_all()
    row2 = await db_session.get(Consultation, uuid.UUID(child["id"]))
    assert row2.reminder_sent_at == first_marked


async def test_send_due_reminders_skips_far_future(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, cid, timedelta(hours=5))  # fuera de la ventana

    await client.post(f"{PREFIX}/consultations/agenda/send-due-reminders")
    db_session.expire_all()
    row = await db_session.get(Consultation, uuid.UUID(child["id"]))
    assert row is not None and row.reminder_sent_at is None


async def test_appointment_email_args_needs_patient_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, cid, timedelta(days=1))
    row = await db_session.get(Consultation, uuid.UUID(child["id"]))

    # Sin email en el paciente → no hay a quién escribir.
    assert await notifications.appointment_email_args(db_session, row) is None

    # Con email → devuelve los args listos para el correo.
    patient = await db_session.get(Patient, row.patient_id)
    patient.email = "pac@example.com"
    await db_session.flush()
    args = await notifications.appointment_email_args(db_session, row)
    assert args is not None
    assert args["to_email"] == "pac@example.com"
    assert args["code"] == child["code"]


async def test_close_saves_signature(client: AsyncClient, db_session: AsyncSession) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid = await _open_consultation(client, doc.id)
    sig = "data:image/png;base64,SIGNATURE_DATA"
    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/close",
        json={"outcome": "closed", "note": "n", "signature": sig},
        headers=auth_headers(doc.id),
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    row = await db_session.get(Consultation, uuid.UUID(cid))
    assert row is not None and row.close_signature == sig


async def test_detail_has_patient_and_events_have_author(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    cid = await _open_consultation(client, doc.id)

    # GET /{id}: el detalle trae el paciente anidado (para el panel, sin leer `patients` directo).
    r = await client.get(f"{PREFIX}/consultations/{cid}", headers=auth_headers(doc.id))
    assert r.status_code == 200, r.text
    patient = r.json()["patient"]
    assert patient is not None
    assert patient["full_name"] == "Pac Agenda"
    assert patient["phone_whatsapp"] == "+58412555222"

    # GET /{id}/events: cada evento trae el autor resuelto (author_name/role).
    await client.post(
        f"{PREFIX}/consultations/{cid}/events",
        json={"consultation_id": cid, "event_type": "admin_update", "note": "n"},
        headers=auth_headers(doc.id),
    )
    ev = await client.get(f"{PREFIX}/consultations/{cid}/events", headers=auth_headers(doc.id))
    assert ev.status_code == 200, ev.text
    mine = [e for e in ev.json() if e["created_by"] == str(doc.id)]
    assert mine and mine[0]["author_name"] == doc.full_name
    assert mine[0]["author_role"] == "doctor"


# --- Iniciar una cita agendada (Agenda): scheduled → in_progress + sala ---
#
# La hija agendada YA EXISTE (la crean schedule-follow-up/refer); lo que faltaba era abrirla. Sin
# este paso `ensure_video_room` responde 409 ("La consulta ya no está abierta.") y el paciente
# —que solo ve el botón de entrar cuando la fase es `ready`— no se entera de nada.


@contextmanager
def _capturar_aviso_de_video():
    """Dobla el envío del aviso "tu médico ya está en la sala" (lo encola el router). Se parchea
    el nombre que el BackgroundTask referencia, igual que en test_consultations."""
    enviados: list[dict] = []

    async def _fake(**kwargs) -> bool:
        enviados.append(kwargs)
        return True

    with patch("src.services.notifications.send_video_ready_email", AsyncMock(side_effect=_fake)):
        yield enviados


async def _scheduled_child_con_correo(
    client: AsyncClient, db_session: AsyncSession, email: str = "agenda@example.com"
) -> tuple[dict, Profile]:
    """Médico + paciente CON correo + consulta tomada y agendada: (cita hija, médico)."""
    doc = await add_doctor(db_session, specialty=GENERAL)
    pid = (
        await client.post(
            f"{PREFIX}/patients",
            json={
                "full_name": "Pac Con Correo",
                "phone_whatsapp": "+58412555333",
                "emergency_phone": "+58414555333",
                "email": email,
                "address_encrypted": "v1:dGVzdCBjaXBoZXJ0ZXh0",
                "affected_zone": "Caracas",
                "consent": True,
            },
        )
    ).json()["id"]
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={
                "patient_id": pid,
                "chief_complaint": "Control",
                "specialty_id": await any_specialty_id(client),
            },
        )
    ).json()["id"]
    await client.post(f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(doc.id))
    return await _schedule_in(client, doc, cid, timedelta(days=1)), doc


async def test_start_scheduled_opens_it_and_creates_the_room(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    parent_cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, parent_cid, timedelta(days=1))

    resp = await client.post(
        f"{PREFIX}/consultations/{child['id']}/start", headers=auth_headers(doc.id)
    )
    assert resp.status_code == 200, resp.text
    opened = resp.json()
    assert opened["status"] == "in_progress"
    assert opened["video_room_url"]  # la sala se crea en el mismo paso
    assert opened["assigned_doctor_id"] == str(doc.id)

    events = (
        await client.get(
            f"{PREFIX}/consultations/{child['id']}/events", headers=auth_headers(doc.id)
        )
    ).json()
    assert any(e["event_type"] == "opened" for e in events)


async def test_start_scheduled_abre_la_sala_para_el_paciente(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    parent_cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, parent_cid, timedelta(days=1))

    # Antes de iniciar: fase agendada, sin enlace (no hay médico dentro de la sala todavía).
    before = (
        await client.get(
            f"{PREFIX}/consultations/{child['id']}/waiting-room", headers=auth_headers(doc.id)
        )
    ).json()
    assert before["phase"] == "scheduled"
    assert before["video_room_url"] is None

    await client.post(f"{PREFIX}/consultations/{child['id']}/start", headers=auth_headers(doc.id))

    after = (
        await client.get(
            f"{PREFIX}/consultations/{child['id']}/waiting-room", headers=auth_headers(doc.id)
        )
    ).json()
    assert after["phase"] == "ready"
    assert after["video_room_url"]
    assert after["doctor_name"] == doc.full_name


async def test_start_scheduled_avisa_al_paciente_por_correo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    child, doc = await _scheduled_child_con_correo(client, db_session)

    with _capturar_aviso_de_video() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations/{child['id']}/start", headers=auth_headers(doc.id)
        )

    assert resp.status_code == 200, resp.text
    assert len(enviados) == 1
    aviso = enviados[0]
    assert aviso["to_email"] == "agenda@example.com"
    # El enlace pasa por el sitio y registra la entrada, igual que en el claim de la cola.
    assert "/entrar-videoconsulta?" in aviso["join_url"]
    assert str(child["id"]) in aviso["join_url"]


async def test_start_scheduled_sin_correo_del_paciente_no_intenta_avisar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`patients.email` es opcional: no tener a dónde escribir no es un fallo."""
    doc = await add_doctor(db_session, specialty=GENERAL)
    parent_cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, parent_cid, timedelta(days=1))

    with _capturar_aviso_de_video() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations/{child['id']}/start", headers=auth_headers(doc.id)
        )

    assert resp.status_code == 200, resp.text
    assert enviados == []


async def test_start_scheduled_rechaza_a_otro_medico(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    other = await add_doctor(db_session, specialty=GENERAL)
    parent_cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, parent_cid, timedelta(days=1))

    resp = await client.post(
        f"{PREFIX}/consultations/{child['id']}/start", headers=auth_headers(other.id)
    )
    assert resp.status_code == 409, resp.text


async def test_start_scheduled_doble_clic_es_409_y_no_duplica(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    parent_cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, parent_cid, timedelta(days=1))

    first = await client.post(
        f"{PREFIX}/consultations/{child['id']}/start", headers=auth_headers(doc.id)
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"{PREFIX}/consultations/{child['id']}/start", headers=auth_headers(doc.id)
    )
    assert second.status_code == 409, second.text
    # La sala del primer clic es la que queda (el segundo no la pisó).
    assert (
        await client.get(f"{PREFIX}/consultations/{child['id']}", headers=auth_headers(doc.id))
    ).json()["video_room_url"] == first.json()["video_room_url"]


async def test_start_scheduled_requiere_permiso_queue_take(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session, specialty=GENERAL)
    parent_cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, parent_cid, timedelta(days=1))
    paciente = make_profile(role="patient")
    db_session.add(paciente)
    await db_session.flush()

    resp = await client.post(
        f"{PREFIX}/consultations/{child['id']}/start", headers=auth_headers(paciente.id)
    )
    assert resp.status_code == 403, resp.text
