"""Tests del módulo Agenda: agendar seguimiento (padre→hija), firma al cerrar, agenda, cadena."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consultation import Consultation
from src.models.patient import Patient
from src.services import notifications
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


async def _open_consultation(client: AsyncClient, doctor_id) -> str:
    """Crea paciente + consulta y la TOMA el médico (queda in_progress, asignada)."""
    p = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Pac Agenda",
            "phone_whatsapp": "+58412555222",
            "affected_zone": "Caracas",
            "consent": True,
        },
    )
    cid = (
        await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": p.json()["id"], "chief_complaint": "Dolor"},
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
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
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
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
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
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
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
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
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
    doc = make_profile(role="doctor")
    specialist = make_profile(role="specialist")
    db_session.add_all([doc, specialist])
    await db_session.flush()
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
    assert child["internal_note"] == "Requiere cardiología"

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


async def test_refer_rejects_self(client: AsyncClient, db_session: AsyncSession) -> None:
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
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
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
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
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
    cid = await _open_consultation(client, doc.id)
    child = await _schedule_in(client, doc, cid, timedelta(hours=5))  # fuera de la ventana

    await client.post(f"{PREFIX}/consultations/agenda/send-due-reminders")
    db_session.expire_all()
    row = await db_session.get(Consultation, uuid.UUID(child["id"]))
    assert row is not None and row.reminder_sent_at is None


async def test_appointment_email_args_needs_patient_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
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
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
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
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()
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
