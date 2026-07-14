"""Pruebas de concurrencia de la cola (Board).

Objetivo: garantizar que dos médicos NUNCA tomen el mismo paciente. El ganador
recibe 200 y el perdedor 409 (o 404 si la carrera se resolvió justo después del
commit del ganador). Nunca debe haber doble asignación ni peticiones colgadas.

Usa sesiones/conexiones reales (no override) para que el bloqueo de filas sea
genuino, y un JWT real de un perfil staff committeado.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from src.db.session import AsyncSessionLocal
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.profile import Profile
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


async def _seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Crea (committed) un médico staff + paciente + consulta en espera."""
    async with AsyncSessionLocal() as s:
        doctor = make_profile(role="doctor", specialty="Medicina general")
        patient = Patient(
            full_name="Cola Test",
            phone_whatsapp="+58412999999",
            affected_zone="Caracas",
            consent=True,
        )
        s.add_all([doctor, patient])
        await s.flush()
        consultation = Consultation(
            code=f"TEST-{uuid.uuid4().hex[:10]}",
            patient_id=patient.id,
            status="waiting",
        )
        s.add(consultation)
        await s.commit()
        return consultation.id, patient.id, doctor.id


async def _cleanup(
    consultation_id: uuid.UUID, patient_id: uuid.UUID, doctor_id: uuid.UUID
) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Consultation).where(Consultation.id == consultation_id))
        await s.execute(delete(Patient).where(Patient.id == patient_id))
        await s.execute(delete(Profile).where(Profile.id == doctor_id))
        await s.commit()


@pytest_asyncio.fixture
async def waiting_case() -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID], None]:
    consultation_id, patient_id, doctor_id = await _seed()
    try:
        yield consultation_id, doctor_id
    finally:
        await _cleanup(consultation_id, patient_id, doctor_id)


async def test_take_locked_row_returns_409(
    live_client: AsyncClient, waiting_case: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Si la fila ya está bloqueada por otra transacción, take() falla rápido con 409."""
    consultation_id, doctor_id = waiting_case

    holder = AsyncSessionLocal()
    conn = await holder.connection()
    await conn.execute(
        select(Consultation).where(Consultation.id == consultation_id).with_for_update()
    )
    try:
        resp = await live_client.post(
            f"{PREFIX}/queue/{consultation_id}/take", headers=auth_headers(doctor_id)
        )
        assert resp.status_code == 409, resp.text
    finally:
        await holder.rollback()
        await holder.close()


async def test_concurrent_take_single_winner(
    live_client: AsyncClient, waiting_case: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """N peticiones simultáneas: exactamente un 200, sin doble asignación."""
    consultation_id, doctor_id = waiting_case
    headers = auth_headers(doctor_id)

    async def take() -> int:
        resp = await live_client.post(f"{PREFIX}/queue/{consultation_id}/take", headers=headers)
        return resp.status_code

    statuses = await asyncio.gather(*[take() for _ in range(5)])

    assert statuses.count(200) == 1, f"Debe haber exactamente un ganador: {statuses}"
    assert all(s in (200, 409, 404) for s in statuses), statuses

    async with AsyncSessionLocal() as s:
        consultation = await s.get(Consultation, consultation_id)
        assert consultation.status == "in_progress"
        assert consultation.assigned_doctor_id == doctor_id
