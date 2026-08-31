"""Pruebas de la interconsulta ASÍNCRONA (ver tasks/interconsulta-asincrona/spec.md).

Este archivo arranca por la capa de datos: las invariantes que impone la TABLA. Pydantic
protege la puerta HTTP, pero un script, una migración futura o un psql a mano entran por
debajo; los CHECK son la última línea. Cada aserción de acá corresponde a un estado imposible
que, de colarse, rompería la vista del especialista o la del tratante.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.interconsultation_request import (
    REQUEST_MODES,
    REQUEST_STATUSES,
    InterconsultationRequest,
)
from src.models.patient import Patient
from src.models.specialty import Specialty
from tests._helpers import make_profile


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
