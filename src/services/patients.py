"""Capa de negocio para patients."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import BadRequestError, ForbiddenError, NotFoundError
from src.models.patient import Patient
from src.schemas.patient import (
    DoctorPatientCreate,
    DoctorPatientUpdate,
    PatientCreate,
    PatientUpdate,
)
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


async def list_patients(
    session: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    include_doctor_patients: bool = False,
) -> list[Patient]:
    """Listado staff. Por defecto **solo pacientes de la cola pública**.

    Los de consultorio (`created_by_doctor_id` no nulo) quedan fuera: nunca entraron a la
    plataforma, y `patients.read` lo tiene TODO médico — sin este filtro, cualquier colega leería
    el nombre, la cédula y las alergias de los pacientes privados de otro. Sería incoherente
    anonimizar el caso en la bandeja de interconsultas y regalar la ficha completa por acá.
    `include_doctor_patients` es para quien tiene `patients.write` (admin), que sí necesita la
    visión completa para operar.
    """
    stmt = select(Patient).where(Patient.deleted_at.is_(None))  # soft delete: no listar archivados
    if not include_doctor_patients:
        stmt = stmt.where(Patient.created_by_doctor_id.is_(None))
    stmt = stmt.order_by(Patient.created_at.desc(), Patient.id).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_patient_as_staff(
    session: AsyncSession, patient_id: uuid.UUID, *, may_see_doctor_patients: bool
) -> Patient:
    """Lectura staff de un paciente. Misma frontera que `list_patients`, aplicada al detalle:
    un guard que cubre el listado pero no el `GET /{id}` no es un guard (lección del sub-recurso
    `/events` en @.claude/rules/security.md)."""
    patient = await get_patient(session, patient_id)
    if patient.created_by_doctor_id is not None and not may_see_doctor_patients:
        raise ForbiddenError(
            "Este paciente es de consultorio: solo lo ve el médico que lo registró."
        )
    return patient


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


# --- Pacientes de consultorio (alta por médico, para pedir una interconsulta) ---
#
# Segunda vía de alta de `patients`, distinta de la pública: la fila lleva
# `created_by_doctor_id` y es del médico que la creó. La pertenencia se valida ACÁ, junto a la
# query, no en el router (regla IDOR de @.claude/rules/security.md): el permiso RBAC autoriza la
# *acción*, nunca el *objeto*.


async def _own_patient(
    session: AsyncSession, patient_id: uuid.UUID, doctor_id: uuid.UUID
) -> Patient:
    """El paciente de consultorio del médico que llama. 404 si no existe o está archivado;
    403 si existe pero es de otro médico (o es un alta pública, que no tiene dueño médico)."""
    patient = await get_patient(session, patient_id)  # 404 incluye el archivado
    if patient.created_by_doctor_id != doctor_id:
        raise ForbiddenError("Este paciente no fue registrado por vos.")
    return patient


async def create_doctor_patient(
    session: AsyncSession, data: DoctorPatientCreate, doctor_id: uuid.UUID
) -> Patient:
    """Da de alta un paciente de consultorio a nombre del médico que llama."""
    if not data.consent:
        raise BadRequestError(
            "Se requiere declarar el consentimiento del paciente (consent = true)."
        )
    patient = Patient(**data.model_dump(), created_by_doctor_id=doctor_id)
    patient.consent_at = datetime.now(UTC)
    session.add(patient)
    await session.flush()
    # Se audita el alta (la pública no lo hace): acá un miembro del staff crea PII de un tercero
    # que no está en la plataforma y no puede reclamar por sí mismo.
    await audit.log_action(
        session,
        action="patient.created_by_doctor",
        actor_user_id=doctor_id,
        resource="patients",
        resource_id=patient.id,
    )
    await session.commit()
    await session.refresh(patient)
    return patient


async def list_doctor_patients(
    session: AsyncSession, doctor_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[Patient]:
    """Los pacientes de consultorio del médico que llama (no archivados)."""
    stmt = (
        select(Patient)
        .where(Patient.created_by_doctor_id == doctor_id, Patient.deleted_at.is_(None))
        # `id` como desempate: sin una columna única al final, dos altas del mismo instante
        # pueden repetirse u omitirse entre páginas con OFFSET.
        .order_by(Patient.created_at.desc(), Patient.id)
        .offset(skip)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_doctor_patient(
    session: AsyncSession, patient_id: uuid.UUID, doctor_id: uuid.UUID
) -> Patient:
    return await _own_patient(session, patient_id, doctor_id)


async def update_doctor_patient(
    session: AsyncSession,
    patient_id: uuid.UUID,
    data: DoctorPatientUpdate,
    doctor_id: uuid.UUID,
) -> Patient:
    patient = await _own_patient(session, patient_id, doctor_id)
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(patient, field, value)
    await audit.log_action(
        session,
        action="patient.updated",
        actor_user_id=doctor_id,
        resource="patients",
        resource_id=patient.id,
        metadata={"fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(patient)
    return patient


async def delete_doctor_patient(
    session: AsyncSession, patient_id: uuid.UUID, doctor_id: uuid.UUID
) -> None:
    """Baja lógica del paciente propio. Nunca hard delete (igual que el resto de la tabla)."""
    patient = await _own_patient(session, patient_id, doctor_id)
    patient.deleted_at = func.now()
    await audit.log_action(
        session,
        action="patient.deleted",
        actor_user_id=doctor_id,
        resource="patients",
        resource_id=patient.id,
    )
    await session.commit()


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
