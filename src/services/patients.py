"""Capa de negocio para patients."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import BadRequestError, NotFoundError
from src.models.patient import Patient
from src.schemas.patient import PatientCreate, PatientUpdate
from src.services import audit


async def _resolve_dependent_cedula(session: AsyncSession, parent_id: uuid.UUID) -> str | None:
    """Cédula sintética para un menor sin cédula propia: cédula del adulto responsable
    + correlativo de carga familiar (1, 2, 3...). P. ej. adulto 24319284 -> primer
    menor 243192841, segundo menor 243192842. Sin cédula en el adulto, no hay base
    para generarla (queda None, no es un error)."""
    guardian = await session.get(Patient, parent_id)
    if guardian is None or not guardian.cedula:
        return None
    dependientes = await session.scalar(
        select(func.count()).select_from(Patient).where(Patient.parent_id == parent_id)
    )
    return f"{guardian.cedula}{(dependientes or 0) + 1}"


async def list_patients(session: AsyncSession, skip: int = 0, limit: int = 100) -> list[Patient]:
    stmt = (
        select(Patient)
        .where(Patient.deleted_at.is_(None))  # soft delete: no listar los archivados
        .order_by(Patient.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_patients_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[Patient]:
    """Registros de paciente ligados a la cuenta del usuario (user_id == caller, no archivados).
    Para el portal del paciente (mi-caso), que no tiene el permiso staff patients.read; replica la
    RLS patients_select_own (user_id = auth.uid())."""
    stmt = (
        select(Patient)
        .where(Patient.user_id == user_id, Patient.deleted_at.is_(None))
        .order_by(Patient.created_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_patient(session: AsyncSession, patient_id: uuid.UUID) -> Patient:
    patient = await session.get(Patient, patient_id)
    if patient is None or patient.deleted_at is not None:  # soft delete: el archivado es 404
        raise NotFoundError("Paciente no encontrado.")
    return patient


async def create_patient(session: AsyncSession, data: PatientCreate) -> Patient:
    if not data.consent:
        raise BadRequestError("Se requiere el consentimiento del paciente (consent = true).")
    if data.parent_id is not None and await session.get(Patient, data.parent_id) is None:
        raise BadRequestError("El adulto responsable referenciado (parent_id) no existe.")
    patient = Patient(**data.model_dump())
    if patient.parent_id is not None and not patient.cedula:
        patient.cedula = await _resolve_dependent_cedula(session, patient.parent_id)
    if patient.consent and patient.consent_at is None:
        patient.consent_at = datetime.now(UTC)
    session.add(patient)
    await session.commit()
    await session.refresh(patient)
    return patient


async def update_patient(
    session: AsyncSession,
    patient_id: uuid.UUID,
    data: PatientUpdate,
    actor_user_id: uuid.UUID | None = None,
) -> Patient:
    patient = await get_patient(session, patient_id)
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(patient, field, value)
    await audit.log_action(
        session,
        action="patient.updated",
        actor_user_id=actor_user_id,
        resource="patients",
        resource_id=patient.id,
        metadata={"fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(patient)
    return patient


async def delete_patient(
    session: AsyncSession, patient_id: uuid.UUID, actor_user_id: uuid.UUID | None = None
) -> None:
    """Baja lógica (soft delete): marca deleted_at, no borra la fila (trazabilidad). Mismo patrón
    que delete_doctor. get_patient ya devuelve 404 si el paciente estaba archivado."""
    patient = await get_patient(session, patient_id)
    patient.deleted_at = func.now()
    await audit.log_action(
        session,
        action="patient.deleted",
        actor_user_id=actor_user_id,
        resource="patients",
        resource_id=patient.id,
    )
    await session.commit()
