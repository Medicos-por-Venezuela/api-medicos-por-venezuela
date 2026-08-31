"""Capa HTTP de la interconsulta asíncrona (pacientes de consultorio).

No confundir con `/interconsultations`, que es la segunda opinión EN VIVO durante una consulta
activa de la cola. Ver .knowledge/interconsultas.md.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.interconsultation_request import (
    InterconsultationRequestCreate,
    InterconsultationRequestResponse,
)
from src.services import interconsultation_requests as requests_service
from src.services.mail import send_bulk

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

_WRITE = "interconsultation_requests.write"


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
    },
)
async def create_request(
    payload: InterconsultationRequestCreate,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> InterconsultationRequestResponse:
    """Crea la solicitud y difunde el aviso por correo.

    En modo `specialty` le llega a todos los médicos habilitados de esa especialidad; en modo
    `doctor`, solo al elegido. `notified_count` dice a cuántos se les avisó.

    El envío va en segundo plano y es best-effort: que un correo falle **no** cambia el 201 ni
    deshace la solicitud."""
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
