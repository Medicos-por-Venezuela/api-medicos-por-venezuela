"""Operaciones CRUD para patients."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.patient import Patient
from app.schemas.patient import PatientCreate, PatientUpdate


def get(db: Session, patient_id: uuid.UUID) -> Patient | None:
    return db.get(Patient, patient_id)


def list_all(db: Session, skip: int = 0, limit: int = 100) -> list[Patient]:
    stmt = select(Patient).order_by(Patient.created_at.desc()).offset(skip).limit(limit)
    return list(db.scalars(stmt).all())


def create(db: Session, data: PatientCreate) -> Patient:
    patient = Patient(**data.model_dump())
    if patient.consent and patient.consent_at is None:
        patient.consent_at = datetime.now(timezone.utc)
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def update(db: Session, patient: Patient, data: PatientUpdate) -> Patient:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(patient, field, value)
    db.commit()
    db.refresh(patient)
    return patient


def delete(db: Session, patient: Patient) -> None:
    db.delete(patient)
    db.commit()
