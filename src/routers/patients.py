"""Capa HTTP (delgada) para patients. La lógica vive en src.services.patients.

Autorización: crear es público (alta del paciente); leer requiere staff; editar y
eliminar requieren admin (replica las RLS).
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.errors import ForbiddenError
from src.core.ratelimit import limiter
from src.core.security import Principal, get_current_principal, require_permission
from src.db.session import get_db
from src.models.patient import Patient
from src.schemas.patient import (
    PatientAddressResponse,
    PatientCreate,
    PatientResponse,
    PatientUpdate,
)
from src.services import patients as patients_service

router = APIRouter(prefix="/patients", tags=["patients"])
tag_metadata = [
    {"name": "patients", "description": "Pacientes (alta con consentimiento, consulta, edición)."}
]

_NOT_FOUND = {404: {"description": "Paciente no encontrado."}}


def _patient_response(patient: Patient, principal: Principal) -> PatientResponse:
    """Serializa el paciente con el teléfono de emergencia solo para quien corresponde.

    Es PII de contacto pedida para emergencias: la ven el equipo admin, el médico dueño del
    paciente de consultorio y el propio paciente. El médico tratante la recibe en el detalle de
    su consulta (GET /consultations/{id}), no por acá."""
    response = PatientResponse.model_validate(patient)
    may_see = (
        principal.is_admin
        or patient.created_by_doctor_id == principal.id
        or patient.user_id == principal.id
    )
    if not may_see:
        response.emergency_phone = None
    return response


@router.get(
    "",
    response_model=list[PatientResponse],
    summary="Listar pacientes (staff)",
    responses={403: {"description": "`scope=all` requiere el permiso patients.write."}},
)
async def list_patients(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    scope: Literal["public", "all"] = Query(
        "public",
        description=(
            "'public' (default) = solo pacientes de la cola pública. 'all' incluye además los "
            "de consultorio registrados por médicos; requiere patients.write."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("patients.read")),
) -> list[PatientResponse]:
    """Lista paginada de pacientes (más recientes primero).

    Por defecto excluye los pacientes **de consultorio**: son privados del médico que los
    registró y `patients.read` lo tiene todo médico. Para verlos hace falta `patients.write`
    (admin) y pedir `scope=all` explícitamente."""
    if scope == "all" and not principal.has_permission("patients.write"):
        raise ForbiddenError(
            "Ver los pacientes de consultorio requiere el permiso patients.write."
        )
    patients = await patients_service.list_patients(
        db, skip=skip, limit=limit, include_doctor_patients=scope == "all"
    )
    return [_patient_response(patient, principal) for patient in patients]


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
    patients = await patients_service.list_patients_for_user(db, principal.id)
    return [_patient_response(patient, principal) for patient in patients]


@router.get(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="Obtener paciente (staff)",
    responses=_NOT_FOUND,
)
async def get_patient(
    patient_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("patients.read")),
) -> PatientResponse:
    """Un paciente de la cola pública. Los de consultorio dan 403 por acá: los lee su médico en
    `/doctors/me/patients/{id}`, o un admin (`patients.write`)."""
    patient = await patients_service.get_patient_as_staff(
        db, patient_id, may_see_doctor_patients=principal.has_permission("patients.write")
    )
    return _patient_response(patient, principal)


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


@router.get(
    "/{patient_id}/address",
    response_model=PatientAddressResponse,
    summary="Dirección cifrada del paciente (solo tratante o allowlist)",
    responses={
        403: {
            "description": (
                "Solo el médico que atiende el caso (o allowlist) puede ver la dirección."
            )
        },
        404: {"description": "Paciente no encontrado."},
    },
)
async def get_patient_address(
    patient_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("patients.read")),
) -> PatientAddressResponse:
    """Devuelve la dirección cifrada E2E (v1:base64 sealed box) del paciente.

    Autorizado solo si:
    - El email del principal está en la allowlist configurable (`ADDRESS_VIEWER_EMAILS`), O
    - El principal es el médico asignado a alguna consulta de este paciente (cualquier estado).

    Escribe auditoría con action="patient.address_revealed" antes de responder.
    El servidor NUNCA descifra, loguea ni valida el contenido.
    """
    patient = await patients_service.get_patient_as_staff(
        db, patient_id, may_see_doctor_patients=principal.has_permission("patients.write")
    )
    address_encrypted = await patients_service.patient_address_for_viewer(
        db, patient, viewer_id=principal.id, viewer_email=principal.email
    )
    return PatientAddressResponse(address_encrypted=address_encrypted)
