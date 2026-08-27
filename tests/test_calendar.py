"""Tests del feed iCalendar de la agenda: URL de suscripción, .ics por token, rotación."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.profile import Profile
from src.services import calendar as calendar_service
from tests._helpers import add_doctor, auth_headers, make_profile

PREFIX = "/api/v1"


async def _open_consultation(client: AsyncClient, doctor_id) -> str:
    p = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Pac Cal",
            "phone_whatsapp": "+58412555999",
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


def _token_from_url(ics_url: str) -> str:
    return ics_url.rsplit("/", 1)[-1].removesuffix(".ics")


async def test_calendar_url_generates_and_persists_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session)
    r = await client.get(f"{PREFIX}/agenda/calendar-url", headers=auth_headers(doc.id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ics_url"].endswith(".ics")
    assert body["webcal_url"].startswith("webcal://")

    token = await db_session.scalar(select(Profile.calendar_token).where(Profile.id == doc.id))
    assert token is not None


async def test_ics_feed_lists_scheduled_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session)
    cid = await _open_consultation(client, doc.id)
    when = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    child = (
        await client.post(
            f"{PREFIX}/consultations/{cid}/schedule-follow-up",
            json={"scheduled_at": when},
            headers=auth_headers(doc.id),
        )
    ).json()

    url = (await client.get(f"{PREFIX}/agenda/calendar-url", headers=auth_headers(doc.id))).json()[
        "ics_url"
    ]
    tok = _token_from_url(url)

    r = await client.get(f"{PREFIX}/agenda/{tok}.ics")
    assert r.status_code == 200, r.text
    assert "text/calendar" in r.headers["content-type"]
    body = r.text
    assert "BEGIN:VCALENDAR" in body
    assert "BEGIN:VEVENT" in body
    assert child["code"] in body


async def test_rotate_revokes_old_token(client: AsyncClient, db_session: AsyncSession) -> None:
    doc = await add_doctor(db_session)
    tok1 = _token_from_url(
        (await client.get(f"{PREFIX}/agenda/calendar-url", headers=auth_headers(doc.id))).json()[
            "ics_url"
        ]
    )
    tok2 = _token_from_url(
        (
            await client.post(f"{PREFIX}/agenda/calendar-url/rotate", headers=auth_headers(doc.id))
        ).json()["ics_url"]
    )
    assert tok1 != tok2
    assert (await client.get(f"{PREFIX}/agenda/{tok1}.ics")).status_code == 404
    assert (await client.get(f"{PREFIX}/agenda/{tok2}.ics")).status_code == 200


async def test_ics_invalid_or_unknown_token_404(client: AsyncClient) -> None:
    assert (await client.get(f"{PREFIX}/agenda/not-a-uuid.ics")).status_code == 404
    assert (await client.get(f"{PREFIX}/agenda/{uuid.uuid4()}.ics")).status_code == 404


async def test_ics_for_patient_user_builds_empty_calendar(db_session: AsyncSession) -> None:
    # Rama del paciente (rol 'patient'): sin citas devuelve un VCALENDAR válido y vacío.
    patient_user = make_profile(role="patient")
    db_session.add(patient_user)
    await db_session.flush()
    ics = await calendar_service.agenda_ics_for_user(db_session, patient_user)
    assert "BEGIN:VCALENDAR" in ics
    assert "END:VCALENDAR" in ics
