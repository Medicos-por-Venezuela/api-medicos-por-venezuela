"""Prueba del mecanismo de liberación de consultas estancadas."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consultation import Consultation
from src.models.patient import Patient

PREFIX = "/api/v1"


async def _in_progress(db_session: AsyncSession, opened_minutes_ago: int) -> uuid.UUID:
    patient = Patient(
        full_name="Estancado",
        phone_whatsapp="+58412909090",
        affected_zone="Caracas",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()
    consultation = Consultation(
        code=f"STALE-{uuid.uuid4().hex[:8]}",
        patient_id=patient.id,
        status="in_progress",
        opened_at=datetime.now(UTC) - timedelta(minutes=opened_minutes_ago),
    )
    db_session.add(consultation)
    await db_session.flush()
    return consultation.id


async def test_release_stale_returns_old_cases_to_queue(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    stale = await _in_progress(db_session, opened_minutes_ago=120)  # vieja
    fresh = await _in_progress(db_session, opened_minutes_ago=1)  # reciente

    resp = await client.post(f"{PREFIX}/queue/release-stale", params={"minutes": 30})
    assert resp.status_code == 200, resp.text
    assert resp.json()["released"] >= 1
    assert resp.json()["threshold_minutes"] == 30

    # La vieja vuelve a 'waiting' (liberada); la reciente sigue 'in_progress'.
    stale_after = await db_session.get(Consultation, stale)
    fresh_after = await db_session.get(Consultation, fresh)
    await db_session.refresh(stale_after)
    await db_session.refresh(fresh_after)
    assert stale_after.status == "waiting"
    assert stale_after.assigned_doctor_id is None
    assert fresh_after.status == "in_progress"
