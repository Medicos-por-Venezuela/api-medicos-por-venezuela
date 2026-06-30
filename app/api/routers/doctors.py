"""Endpoints REST para doctors."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.crud import doctors as crud_doctors
from app.db.session import get_db
from app.schemas.doctor import DoctorCreate, DoctorRead, DoctorUpdate

router = APIRouter(prefix="/doctors", tags=["doctors"])


@router.get("", response_model=list[DoctorRead])
def list_doctors(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
) -> list[DoctorRead]:
    return crud_doctors.list_all(db, skip=skip, limit=limit, status=status_filter)


@router.post("", response_model=DoctorRead, status_code=status.HTTP_201_CREATED)
def create_doctor(payload: DoctorCreate, db: Session = Depends(get_db)) -> DoctorRead:
    return crud_doctors.create(db, payload)


@router.get("/{doctor_id}", response_model=DoctorRead)
def get_doctor(doctor_id: uuid.UUID, db: Session = Depends(get_db)) -> DoctorRead:
    doctor = crud_doctors.get(db, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Médico no encontrado.")
    return doctor


@router.patch("/{doctor_id}", response_model=DoctorRead)
def update_doctor(
    doctor_id: uuid.UUID, payload: DoctorUpdate, db: Session = Depends(get_db)
) -> DoctorRead:
    doctor = crud_doctors.get(db, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Médico no encontrado.")
    return crud_doctors.update(db, doctor, payload)


@router.delete("/{doctor_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_doctor(doctor_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    doctor = crud_doctors.get(db, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Médico no encontrado.")
    crud_doctors.delete(db, doctor)
