"""Capa HTTP (delgada) para doctors. La lógica vive en src.services.doctors.

Autorización:
- Registrar (`POST`): público (auto-registro). El backend verifica la credencial
  contra SACS/FPV y fija `verified`; ese es el control real de acceso.
- Leer: staff. Editar/eliminar: admin.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.ratelimit import limiter
from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.doctor import DoctorCreate, DoctorPoolPage, DoctorResponse, DoctorUpdate
from src.services import doctors as doctors_service

router = APIRouter(prefix="/doctors", tags=["doctors"])
tag_metadata = [
    {"name": "doctors", "description": "Médicos y psicólogos: registro con verificación SACS/FPV."}
]

_NOT_FOUND = {404: {"description": "Médico no encontrado."}}


@router.get("", response_model=list[DoctorResponse], summary="Listar médicos (staff)")
async def list_doctors(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    status_filter: int | None = Query(None, alias="status", ge=0, le=2),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("doctors.read")),
) -> list[DoctorResponse]:
    """Lista de médicos (no borrados); filtrable por `status` (0/1/2)."""
    return await doctors_service.list_doctors(db, skip=skip, limit=limit, status=status_filter)


@router.post(
    "",
    response_model=DoctorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar médico (público; verifica en SACS/FPV)",
    responses={
        422: {"description": "Formato de cédula/teléfono inválido."},
        429: {"description": "Demasiados registros desde esta IP (rate limit)."},
    },
)
@limiter.limit(settings.DOCTOR_REGISTER_RATE_LIMIT)
async def register_doctor(
    request: Request, payload: DoctorCreate, db: AsyncSession = Depends(get_db)
) -> DoctorResponse:
    """Registra un médico/psicólogo. El backend valida la cédula contra el SACS
    (médico) o la FPV (psicólogo) según el `professional_type`; `verified` queda en
    `true` solo si la credencial es válida, `false` en caso contrario.

    Anti-bot: rate limit por IP + campo honeypot (`website`, debe ir vacío)."""
    return await doctors_service.create_doctor(db, payload)


# NOTA: debe ir ANTES de "/{doctor_id}" o FastAPI intenta parsear "pool" como UUID (422).
@router.get(
    "/pool",
    response_model=DoctorPoolPage,
    summary="Pool de médicos para referir/agendar (paginado, con estado online)",
)
async def doctor_pool(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    specialty_id: uuid.UUID | None = Query(None),
    professional_type_id: uuid.UUID | None = Query(None),
    online: bool | None = Query(None, description="true=logeados · false=offline · omitir=todos"),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.read")),
) -> DoctorPoolPage:
    """Médicos activos (status=1) para referir/agendar durante una consulta, con su estado
    online (logeado < 3 min) y teléfono de contacto. Filtrable por especialidad y tipo de
    profesional; los online van primero. Excluye al propio médico que consulta. Devuelve
    `{items, total}` para la paginación del cliente."""
    items, total = await doctors_service.list_doctor_pool(
        db,
        skip=skip,
        limit=limit,
        specialty_id=specialty_id,
        professional_type_id=professional_type_id,
        online=online,
        exclude_user_id=principal.id,
    )
    return DoctorPoolPage(items=items, total=total)


@router.get(
    "/{doctor_id}",
    response_model=DoctorResponse,
    summary="Obtener médico (staff)",
    responses=_NOT_FOUND,
)
async def get_doctor(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("doctors.read")),
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
    _: Principal = Depends(require_permission("doctors.write")),
) -> DoctorResponse:
    return await doctors_service.update_doctor(db, doctor_id, payload)


@router.delete(
    "/{doctor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar médico (admin, baja lógica)",
    responses=_NOT_FOUND,
)
async def delete_doctor(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("doctors.write")),
) -> None:
    await doctors_service.delete_doctor(db, doctor_id)
