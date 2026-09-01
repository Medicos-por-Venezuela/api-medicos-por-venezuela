"""Capa HTTP de la interconsulta asíncrona (pacientes de consultorio).

No confundir con `/interconsultations`, que es la segunda opinión EN VIVO durante una consulta
activa de la cola. Ver .knowledge/interconsultas.md.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.ratelimit import limiter
from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.interconsultation_request import (
    InterconsultationRequestClose,
    InterconsultationRequestCreate,
    InterconsultationRequestInbox,
    InterconsultationRequestResponse,
    InterconsultationRequestTaken,
)
from src.services import interconsultation_requests as requests_service
from src.services.mail import send_bulk, send_mail

router = APIRouter(prefix="/interconsultation-requests", tags=["interconsultation-requests"])
tag_metadata = [
    {
        "name": "interconsultation-requests",
        "description": (
            "Interconsulta asíncrona: un médico pide una segunda opinión sobre un paciente de "
            "su consultorio, por especialidad o a un médico concreto."
        ),
    }
]

_WRITE = "interconsultation_requests.write"  # pedir ayuda
_TAKE = "interconsultation_requests.take"  # darla


@router.post(
    "",
    response_model=InterconsultationRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Solicitar una interconsulta",
    responses={
        403: {"description": "El paciente no fue registrado por quien llama."},
        404: {"description": "Paciente o especialidad no encontrados."},
        409: {"description": "No puedes pedirte una interconsulta a vos mismo."},
        422: {
            "description": (
                "Especialidad no pedible en interconsulta (p. ej. Medicina general), médico "
                "destino no habilitado o sin especialidad, o payload incoherente con `mode`."
            )
        },
        429: {"description": "Demasiadas solicitudes en poco tiempo (rate limit)."},
    },
)
@limiter.limit(settings.INTERCONSULTATION_REQUEST_RATE_LIMIT)
async def create_request(
    request: Request,
    payload: InterconsultationRequestCreate,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> InterconsultationRequestResponse:
    """Crea la solicitud y difunde el aviso por correo.

    En modo `specialty` le llega a todos los médicos habilitados de esa especialidad; en modo
    `doctor`, solo al elegido. `notified_count` dice a cuántos se les avisó.

    El envío va en segundo plano y es best-effort: que un correo falle **no** cambia el 201 ni
    deshace la solicitud.

    `request` es obligatorio para slowapi (lee la IP), aunque no se use aquí. El tope existe
    porque una sola petición dispara hasta `MAIL_FANOUT_MAX` correos a médicos reales."""
    response, difusion = await requests_service.create_request(db, payload, principal.id)
    background.add_task(send_bulk, **difusion)
    return response


@router.get(
    "/mine",
    response_model=list[InterconsultationRequestResponse],
    summary="Mis solicitudes de interconsulta",
)
async def list_my_requests(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> list[InterconsultationRequestResponse]:
    """Las solicitudes que hizo quien llama, más recientes primero.

    Si un caso ya fue tomado, incluye la identidad y el contacto del especialista — que es el
    objetivo del flujo: que los dos médicos se hablen."""
    return await requests_service.list_mine(db, principal.id, skip=skip, limit=limit)


@router.get(
    "/inbox",
    response_model=list[InterconsultationRequestInbox],
    summary="Bandeja: casos abiertos para mi especialidad",
    responses={403: {"description": "Requiere el permiso interconsultation_requests.take."}},
)
async def inbox(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_TAKE)),
) -> list[InterconsultationRequestInbox]:
    """Solicitudes ABIERTAS de tu especialidad, más las dirigidas a vos.

    **Anonimizadas**: motivo, notas y rango etario del paciente. Ni su identidad ni la del
    médico que pide — el caso se elige por el caso."""
    return await requests_service.inbox(db, principal.id, skip=skip, limit=limit)


@router.get(
    "/taken-by-me",
    response_model=list[InterconsultationRequestTaken],
    summary="Casos activos que tomé",
)
async def taken_by_me(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_TAKE)),
) -> list[InterconsultationRequestTaken]:
    """Los casos que tomaste y siguen abiertos, con el contacto del médico tratante. Sin esta
    lista perderías ese contacto al recargar la página."""
    return await requests_service.taken_by_me(db, principal.id, skip=skip, limit=limit)


@router.post(
    "/{request_id}/take",
    response_model=InterconsultationRequestTaken,
    summary="Tomar una solicitud de interconsulta",
    responses={
        403: {"description": "La interconsulta no es para tu especialidad."},
        404: {"description": "Solicitud no encontrada."},
        409: {"description": "Otro especialista ya la tomó, o ya no está abierta."},
    },
)
async def take_request(
    request_id: uuid.UUID,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_TAKE)),
) -> InterconsultationRequestTaken:
    """Asigna la solicitud a quien llama y le devuelve el contacto del médico tratante.

    Carrera resuelta con bloqueo de fallo rápido: si dos especialistas la toman a la vez, uno
    recibe 200 y el otro **409** de inmediato, sin quedarse colgado."""
    respuesta, aviso = await requests_service.take(db, request_id, principal.id)
    if aviso:
        background.add_task(send_mail, **aviso)
    return respuesta


@router.post(
    "/{request_id}/cancel",
    response_model=InterconsultationRequestResponse,
    summary="Cancelar mi solicitud (aún sin tomar)",
    responses={
        403: {"description": "La solicitud no es tuya."},
        404: {"description": "Solicitud no encontrada."},
        409: {"description": "Ya fue tomada: no se puede cancelar."},
    },
)
async def cancel_request(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> InterconsultationRequestResponse:
    """Retira una solicitud que nadie tomó todavía. Solo el médico que la creó."""
    return await requests_service.cancel(db, request_id, principal.id)


@router.post(
    "/{request_id}/close",
    response_model=InterconsultationRequestResponse,
    summary="Cerrar mi caso (solo el médico tratante)",
    responses={
        403: {"description": "La solicitud no es tuya (el especialista NO cierra el caso)."},
        404: {"description": "Solicitud no encontrada."},
        409: {"description": "Solo se cierra un caso que un especialista haya tomado."},
    },
)
async def close_request(
    request_id: uuid.UUID,
    payload: InterconsultationRequestClose | None = None,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> InterconsultationRequestResponse:
    """Cierra un caso ya tomado, con una nota opcional.

    Es exclusivo del médico **tratante**: el especialista no cierra ni suelta el caso. Quien
    sabe si la ayuda sirvió es quien la pidió."""
    return await requests_service.close(
        db, request_id, principal.id, payload.closing_note if payload else None
    )
