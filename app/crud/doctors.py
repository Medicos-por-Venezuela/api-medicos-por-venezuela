"""Operaciones CRUD para doctors."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.doctor import Doctor
from app.schemas.doctor import DoctorCreate, DoctorUpdate


def get(db: Session, doctor_id: uuid.UUID) -> Doctor | None:
    return db.get(Doctor, doctor_id)


def list_all(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
) -> list[Doctor]:
    stmt = select(Doctor)
    if status:
        stmt = stmt.where(Doctor.status == status)
    stmt = stmt.order_by(Doctor.created_at.desc()).offset(skip).limit(limit)
    return list(db.scalars(stmt).all())


def create(db: Session, data: DoctorCreate) -> Doctor:
    doctor = Doctor(**data.model_dump())
    db.add(doctor)
    db.commit()
    db.refresh(doctor)
    return doctor


def update(db: Session, doctor: Doctor, data: DoctorUpdate) -> Doctor:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(doctor, field, value)
    db.commit()
    db.refresh(doctor)
    return doctor


def delete(db: Session, doctor: Doctor) -> None:
    db.delete(doctor)
    db.commit()
