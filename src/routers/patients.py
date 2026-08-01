"""Capa HTTP (delgada) para patients. La lógica vive en src.services.patients.

Autorización: crear es público (alta del paciente); leer requiere staff; editar y
eliminar requieren admin (replica las RLS).
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.ratelimit import limiter
from src.core.security import Principal, get_current_principal, require_permission
from src.db.session import get_db
from src.schemas.patient import PatientCreate, PatientResponse, PatientUpdate
from src.services import patients as patients_service

router = APIRouter(prefix="/patients", tags=["patients"])
tag_metadata = [
    {"name": "patients", "description": "Pacientes (alta con consentimiento, consulta, edición)."}
]

_NOT_FOUND = {404: {"description": "Paciente no encontrado."}}


@router.get("", response_model=list[PatientResponse], summary="Listar pacientes (staff)")
async def list_patients(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("patients.read")),
) -> list[PatientResponse]:
    """Lista paginada de pacientes (más recientes primero)."""
    return await patients_service.list_patients(db, skip=skip, limit=limit)


@router.post(
    "",
    response_model=PatientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crear paciente (público)",
    responses={
        400: {"description": "Falta el consentimiento (`consent = true`)."},
        429: {"description": "Demasiadas altas desde esta IP (rate limit)."},
    },
)
@limiter.limit(settings.PUBLIC_WRITE_RATE_LIMIT)
async def create_patient(
    request: Request, payload: PatientCreate, db: AsyncSession = Depends(get_db)
) -> PatientResponse:
    """Crea un paciente. Requiere `consent = true` (igual que la política RLS pública).

    `request` es obligatorio para slowapi (lee la IP del cliente), aunque no se use aquí."""
    return await patients_service.create_patient(db, payload)


@router.get(
    "/me",
    response_model=list[PatientResponse],
    summary="Mis registros de paciente (portal del paciente)",
)
async def list_my_patients(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[PatientResponse]:
    """Registros de paciente ligados a la cuenta del llamante (mi-caso). Replica la RLS
    patients_select_own (user_id = auth.uid()); no requiere el permiso staff patients.read.
    Debe ir ANTES de /{patient_id} o FastAPI intenta parsear 'me' como UUID (422)."""
    return await patients_service.list_patients_for_user(db, principal.id)


@router.get(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="Obtener paciente (staff)",
    responses=_NOT_FOUND,
)
async def get_patient(
    patient_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("patients.read")),
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
    principal: Principal = Depends(require_permission("patients.write")),
) -> PatientResponse:
    return await patients_service.update_patient(
        db, patient_id, payload, actor_user_id=principal.id
    )


@router.delete(
    "/{patient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archivar paciente (baja lógica, admin)",
    responses=_NOT_FOUND,
)
async def delete_patient(
    patient_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    # Baja lógica (soft delete), no hard delete: se gatea con patients.write, igual que doctors.
    principal: Principal = Depends(require_permission("patients.write")),
) -> None:
    await patients_service.delete_patient(db, patient_id, actor_user_id=principal.id)
