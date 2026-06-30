"""Endpoints REST para patients."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.crud import patients as crud_patients
from app.db.session import get_db
from app.schemas.patient import PatientCreate, PatientRead, PatientUpdate

router = APIRouter(prefix="/patients", tags=["patients"])


@router.get("", response_model=list[PatientRead])
def list_patients(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[PatientRead]:
    return crud_patients.list_all(db, skip=skip, limit=limit)


@router.post("", response_model=PatientRead, status_code=status.HTTP_201_CREATED)
def create_patient(payload: PatientCreate, db: Session = Depends(get_db)) -> PatientRead:
    if not payload.consent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Se requiere el consentimiento del paciente (consent = true).",
        )
    return crud_patients.create(db, payload)


@router.get("/{patient_id}", response_model=PatientRead)
def get_patient(patient_id: uuid.UUID, db: Session = Depends(get_db)) -> PatientRead:
    patient = crud_patients.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente no encontrado.")
    return patient


@router.patch("/{patient_id}", response_model=PatientRead)
def update_patient(
    patient_id: uuid.UUID, payload: PatientUpdate, db: Session = Depends(get_db)
) -> PatientRead:
    patient = crud_patients.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente no encontrado.")
    return crud_patients.update(db, patient, payload)


@router.delete("/{patient_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_patient(patient_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    patient = crud_patients.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente no encontrado.")
    crud_patients.delete(db, patient)
