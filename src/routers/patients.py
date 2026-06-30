"""Capa HTTP (delgada) para patients. La lógica vive en src.services.patients.

Autorización: crear es público (alta del paciente); leer requiere staff; editar y
eliminar requieren admin (replica las RLS).
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_admin, require_staff
from src.db.session import get_db
from src.schemas.patient import PatientCreate, PatientResponse, PatientUpdate
from src.services import patients as patients_service

router = APIRouter(prefix="/patients", tags=["patients"])

_NOT_FOUND = {404: {"description": "Paciente no encontrado."}}


@router.get("", response_model=list[PatientResponse], summary="Listar pacientes (staff)")
async def list_patients(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_staff),
) -> list[PatientResponse]:
    """Lista paginada de pacientes (más recientes primero)."""
    return await patients_service.list_patients(db, skip=skip, limit=limit)


@router.post(
    "",
    response_model=PatientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear paciente (público)",
    responses={400: {"description": "Falta el consentimiento (`consent = true`)."}},
)
async def create_patient(
    payload: PatientCreate, db: AsyncSession = Depends(get_db)
) -> PatientResponse:
    """Crea un paciente. Requiere `consent = true` (igual que la política RLS pública)."""
    return await patients_service.create_patient(db, payload)


@router.get(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="Obtener paciente (staff)",
    responses=_NOT_FOUND,
)
async def get_patient(
    patient_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_staff),
) -> PatientResponse:
    return await patients_service.get_patient(db, patient_id)


@router.patch(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="Actualizar paciente (admin)",
    responses=_NOT_FOUND,
)
async def update_patient(
    patient_id: uuid.UUID,
    payload: PatientUpdate,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> PatientResponse:
    return await patients_service.update_patient(db, patient_id, payload)


@router.delete(
    "/{patient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar paciente (admin)",
    responses=_NOT_FOUND,
)
async def delete_patient(
    patient_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> None:
    await patients_service.delete_patient(db, patient_id)
