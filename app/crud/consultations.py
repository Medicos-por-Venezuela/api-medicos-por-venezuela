"""Operaciones CRUD para consultations."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.consultation import Consultation
from app.schemas.consultation import ConsultationCreate, ConsultationUpdate


def get(db: Session, consultation_id: uuid.UUID) -> Consultation | None:
    return db.get(Consultation, consultation_id)


def get_by_code(db: Session, code: str) -> Consultation | None:
    return db.scalar(select(Consultation).where(Consultation.code == code))


def list_all(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    patient_id: uuid.UUID | None = None,
) -> list[Consultation]:
    stmt = select(Consultation)
    if status:
        stmt = stmt.where(Consultation.status == status)
    if patient_id:
        stmt = stmt.where(Consultation.patient_id == patient_id)
    stmt = stmt.order_by(Consultation.created_at.desc()).offset(skip).limit(limit)
    return list(db.scalars(stmt).all())


def create(db: Session, data: ConsultationCreate) -> Consultation:
    payload = data.model_dump()
    # Si no se envía code, lo asigna el trigger generate_consultation_code.
    if payload.get("code") is None:
        payload.pop("code", None)
    consultation = Consultation(**payload)
    db.add(consultation)
    db.commit()
    db.refresh(consultation)
    return consultation


def update(db: Session, consultation: Consultation, data: ConsultationUpdate) -> Consultation:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(consultation, field, value)
    db.commit()
    db.refresh(consultation)
    return consultation


def delete(db: Session, consultation: Consultation) -> None:
    db.delete(consultation)
    db.commit()
