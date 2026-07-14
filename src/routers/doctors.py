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
from src.core.security import Principal, get_current_principal, require_permission
from src.db.session import get_db
from src.schemas.doctor import (
    DoctorContactResponse,
    DoctorCreate,
    DoctorMeResponse,
    DoctorPoolPage,
    DoctorResponse,
    DoctorSelfUpdate,
    DoctorUpdate,
)
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


# --- Perfil propio del médico autenticado (self-service) ---
# Declarados ANTES de "/{doctor_id}" para que "me" no se interprete como UUID.


@router.get(
    "/me",
    response_model=DoctorMeResponse,
    summary="Ver mi perfil de médico",
    responses={404: {"description": "No tienes un perfil de médico."}},
)
async def get_my_doctor(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> DoctorMeResponse:
    """Perfil del médico autenticado (identidad tomada del JWT). Devuelve la fila en
    `doctors`; si no existe (médicos que entraron por Google/`finalize-role`), cae a
    la cuenta en `users`. IDOR-safe: el recurso sale del token, nunca de la URL."""
    return await doctors_service.get_my_profile(db, principal.id)


@router.patch(
    "/me",
    response_model=DoctorMeResponse,
    summary="Actualizar mi perfil de médico",
    responses={
        404: {"description": "No tienes un perfil de médico."},
        409: {"description": "La cédula ya pertenece a otro médico."},
        422: {
            "description": (
                "Datos inválidos, campos no permitidos (status/verified/email/phone) o "
                "falta `professional_type_id` para verificar la cédula (cuenta sin ficha)."
            )
        },
    },
)
async def update_my_doctor(
    payload: DoctorSelfUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> DoctorMeResponse:
    """Auto-edición de nombre, licencia, especialidad y cédula.

    - Con ficha (`source:"doctor"`): cambiar la cédula re-verifica contra SACS/FPV y
      recalcula `verified`. No permite tocar `status`/`verified`/`email`/`phone` ni el
      tipo profesional.
    - Sin ficha (`source:"user"`, médico de Google): enviar `cedula` + `professional_type_id`
      verifica la credencial y **crea** la ficha en `doctors`, promoviendo la cuenta a
      `source:"doctor"` (`verified` según SACS/FPV)."""
    return await doctors_service.update_my_profile(db, principal.id, payload)


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
    search: str | None = Query(None, description="Filtra por nombre (ILIKE)."),
    online: bool | None = Query(
        None, description="true=solo online · false=solo offline · omitir=todos"
    ),
    online_ids: list[uuid.UUID] | None = Query(
        None, description="user_ids que el cliente sabe online por Presence (para filtrar online)."
    ),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.read")),
) -> DoctorPoolPage:
    """Médicos activos (status=1) para referir/agendar durante una consulta. Filtrable por nombre
    (`search`), especialidad y tipo. El estado online lo resuelve el frontend con Realtime Presence
    y lo pasa como `online_ids` + `online` (true/false) para filtrar sin romper la paginación. NO
    trae el teléfono (se revela con POST .../contact). Excluye al propio médico que consulta."""
    items, total = await doctors_service.list_doctor_pool(
        db,
        skip=skip,
        limit=limit,
        specialty_id=specialty_id,
        professional_type_id=professional_type_id,
        search=search,
        online=online,
        online_user_ids=online_ids,
        exclude_user_id=principal.id,
    )
    return DoctorPoolPage(items=items, total=total)


@router.post(
    "/{doctor_id}/contact",
    response_model=DoctorContactResponse,
    summary="Revelar el WhatsApp de un médico del pool (queda auditado)",
    responses=_NOT_FOUND,
)
async def reveal_doctor_contact(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.read")),
) -> DoctorContactResponse:
    """Devuelve el teléfono de contacto del médico y REGISTRA en audit_log que este usuario lo vio
    (para la bitácora del panel admin). El número no aparece en el listado del pool: solo aquí."""
    phone = await doctors_service.reveal_doctor_contact(db, doctor_id, viewer_user_id=principal.id)
    return DoctorContactResponse(phone=phone)


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
    principal: Principal = Depends(require_permission("doctors.write")),
) -> DoctorResponse:
    return await doctors_service.update_doctor(db, doctor_id, payload, actor_user_id=principal.id)


@router.delete(
    "/{doctor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar médico (admin, baja lógica)",
    responses=_NOT_FOUND,
)
async def delete_doctor(
    doctor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("doctors.write")),
) -> None:
    await doctors_service.delete_doctor(db, doctor_id, actor_user_id=principal.id)
