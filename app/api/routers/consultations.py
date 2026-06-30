"""Endpoints REST para consultations y sus eventos."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.crud import consultation_events as crud_events
from app.crud import consultations as crud_consultations
from app.crud import patients as crud_patients
from app.db.session import get_db
from app.schemas.consultation import (
    CONSULTATION_STATUSES,
    ConsultationCreate,
    ConsultationRead,
    ConsultationUpdate,
)
from app.schemas.consultation_event import ConsultationEventCreate, ConsultationEventRead

router = APIRouter(prefix="/consultations", tags=["consultations"])


def _validate_status(value: str | None) -> None:
    if value is not None and value not in CONSULTATION_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Estado inválido. Permitidos: {sorted(CONSULTATION_STATUSES)}",
        )


@router.get("", response_model=list[ConsultationRead])
def list_consultations(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    status_filter: str | None = Query(None, alias="status"),
    patient_id: uuid.UUID | None = Query(None),
    db: Session = Depends(get_db),
) -> list[ConsultationRead]:
    _validate_status(status_filter)
    return crud_consultations.list_all(
        db, skip=skip, limit=limit, status=status_filter, patient_id=patient_id
    )


@router.post("", response_model=ConsultationRead, status_code=status.HTTP_201_CREATED)
def create_consultation(
    payload: ConsultationCreate, db: Session = Depends(get_db)
) -> ConsultationRead:
    _validate_status(payload.status)
    if crud_patients.get(db, payload.patient_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El paciente referenciado (patient_id) no existe.",
        )
    if payload.code and crud_consultations.get_by_code(db, payload.code) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe una consulta con ese código.",
        )
    return crud_consultations.create(db, payload)


@router.get("/{consultation_id}", response_model=ConsultationRead)
def get_consultation(consultation_id: uuid.UUID, db: Session = Depends(get_db)) -> ConsultationRead:
    consultation = crud_consultations.get(db, consultation_id)
    if consultation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta no encontrada.")
    return consultation


@router.patch("/{consultation_id}", response_model=ConsultationRead)
def update_consultation(
    consultation_id: uuid.UUID, payload: ConsultationUpdate, db: Session = Depends(get_db)
) -> ConsultationRead:
    _validate_status(payload.status)
    consultation = crud_consultations.get(db, consultation_id)
    if consultation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta no encontrada.")
    return crud_consultations.update(db, consultation, payload)


@router.delete("/{consultation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_consultation(consultation_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    consultation = crud_consultations.get(db, consultation_id)
    if consultation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta no encontrada.")
    crud_consultations.delete(db, consultation)


# --- Eventos / auditoría de la consulta ---


@router.get("/{consultation_id}/events", response_model=list[ConsultationEventRead])
def list_consultation_events(
    consultation_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[ConsultationEventRead]:
    if crud_consultations.get(db, consultation_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta no encontrada.")
    return crud_events.list_by_consultation(db, consultation_id)


@router.post(
    "/{consultation_id}/events",
    response_model=ConsultationEventRead,
    status_code=status.HTTP_201_CREATED,
)
def create_consultation_event(
    consultation_id: uuid.UUID,
    payload: ConsultationEventCreate,
    db: Session = Depends(get_db),
) -> ConsultationEventRead:
    if crud_consultations.get(db, consultation_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta no encontrada.")
    if payload.consultation_id != consultation_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El consultation_id del cuerpo no coincide con el de la ruta.",
        )
    return crud_events.create(db, payload)
