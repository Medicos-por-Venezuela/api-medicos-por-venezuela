"""Capa HTTP (delgada) para doctors. La lógica vive en src.services.doctors.

Autorización: leer requiere staff; crear/editar/eliminar requieren admin.
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_admin, require_staff
from src.db.session import get_db
from src.schemas.doctor import DoctorCreate, DoctorResponse, DoctorUpdate
from src.services import doctors as doctors_service

router = APIRouter(prefix="/doctors", tags=["doctors"])
tag_metadata = [{"name": "doctors", "description": "Médicos voluntarios (directorio operativo)."}]

_NOT_FOUND = {404: {"description": "Médico no encontrado."}}


@router.get("", response_model=list[DoctorResponse], summary="Listar médicos (staff)")
async def list_doctors(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_staff),
) -> list[DoctorResponse]:
    """Lista de médicos; filtrable por `status` (p. ej. `active`)."""
    return await doctors_service.list_doctors(db, skip=skip, limit=limit, status=status_filter)


@router.post(
    "",
    response_model=DoctorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear médico (admin)",
)
async def create_doctor(
    payload: DoctorCreate,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> DoctorResponse:
    return await doctors_service.create_doctor(db, payload)


@router.get(
    "/{doctor_id}",
    response_model=DoctorResponse,
    summary="Obtener médico (staff)",
    responses=_NOT_FOUND,
)
async def get_doctor(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_staff),
) -> DoctorResponse:
    return await doctors_service.get_doctor(db, doctor_id)


@router.patch(
    "/{doctor_id}",
    response_model=DoctorResponse,
    summary="Actualizar médico (admin)",
    responses=_NOT_FOUND,
)
async def update_doctor(
    doctor_id: uuid.UUID,
    payload: DoctorUpdate,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> DoctorResponse:
    return await doctors_service.update_doctor(db, doctor_id, payload)


@router.delete(
    "/{doctor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar médico (admin)",
    responses=_NOT_FOUND,
)
async def delete_doctor(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> None:
    await doctors_service.delete_doctor(db, doctor_id)
