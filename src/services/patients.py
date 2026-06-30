"""Capa de negocio para patients."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import BadRequestError, NotFoundError
from src.models.patient import Patient
from src.schemas.patient import PatientCreate, PatientUpdate


async def list_patients(session: AsyncSession, skip: int = 0, limit: int = 100) -> list[Patient]:
    stmt = select(Patient).order_by(Patient.created_at.desc()).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_patient(session: AsyncSession, patient_id: uuid.UUID) -> Patient:
    patient = await session.get(Patient, patient_id)
    if patient is None:
        raise NotFoundError("Paciente no encontrado.")
    return patient


async def create_patient(session: AsyncSession, data: PatientCreate) -> Patient:
    if not data.consent:
        raise BadRequestError("Se requiere el consentimiento del paciente (consent = true).")
    patient = Patient(**data.model_dump())
    if patient.consent and patient.consent_at is None:
        patient.consent_at = datetime.now(UTC)
    session.add(patient)
    await session.commit()
    await session.refresh(patient)
    return patient


async def update_patient(
    session: AsyncSession, patient_id: uuid.UUID, data: PatientUpdate
) -> Patient:
    patient = await get_patient(session, patient_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(patient, field, value)
    await session.commit()
    await session.refresh(patient)
    return patient


async def delete_patient(session: AsyncSession, patient_id: uuid.UUID) -> None:
    patient = await get_patient(session, patient_id)
    await session.delete(patient)
    await session.commit()
