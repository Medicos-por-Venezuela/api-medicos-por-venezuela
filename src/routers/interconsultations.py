"""Capa HTTP de Interconsultas: segunda opinión EN TIEMPO REAL durante una consulta activa.

Ver .knowledge/interconsultas.md. La lógica y las guardas viven en services/interconsultations.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.observability import client_ip
from src.core.security import Principal, require_staff
from src.db.session import get_db
from src.schemas.interconsultation import (
    InterconsultationCreate,
    InterconsultationForInvitee,
    InterconsultationResponse,
)
from src.services import interconsultations as interconsultations_service
from src.services import notifications

router = APIRouter(prefix="/interconsultations", tags=["interconsultations"])
tag_metadata = [
    {
        "name": "interconsultations",
        "description": "Interconsultas: segunda opinión en vivo (la consulta sigue abierta).",
    }
]


@router.post(
    "",
    response_model=InterconsultationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Asignar una interconsulta a un médico del pool",
    responses={
        403: {"description": "Solo el médico que atiende la consulta puede asignarla."},
        404: {"description": "Consulta no encontrada."},
        409: {"description": "La consulta ya tiene interconsulta, o te asignas a ti mismo."},
    },
)
async def create_interconsultation(
    payload: InterconsultationCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_staff),
) -> InterconsultationResponse:
    """El médico que ATIENDE invita a un médico del pool. La consulta sigue abierta y ambos
    comparten el video. 1 interconsulta por consulta (por ahora).

    La `note` se guarda cifrada y vuelve en claro solo al médico que atiende (si ejerce)."""
    inter = await interconsultations_service.create_interconsultation(
        db,
        consultation_id=payload.consultation_id,
        invited_doctor_id=payload.invited_doctor_id,
        principal=principal,
        note=payload.note,
        ip=client_ip(request),
    )
    # Email "te asignaron una interconsulta" al invitado (si lo tiene habilitado; opt-out). Sin PII
    # del paciente (la interconsulta es de datos limitados).
    args = await notifications.doctor_event_email_args(
        db,
        user_id=payload.invited_doctor_id,
        event="interconsultation_assigned",
        subject="Nueva interconsulta asignada",
        text=(
            "Un colega te invitó a una interconsulta (segunda opinión en vivo).\n\n"
            "Ingresa a tu panel en Médicos por Venezuela para ver el motivo y unirte.\n"
        ),
    )
    if args:
        background_tasks.add_task(notifications.send_mail, **args)
    return inter


# NOTA: /me debe ir ANTES de /for-consultation/{id} para que no se intente parsear "me" como UUID.
@router.get(
    "/me",
    response_model=list[InterconsultationForInvitee],
    summary="Mis interconsultas asignadas (médico invitado; datos limitados)",
)
async def my_interconsultations(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_staff),
) -> list[InterconsultationForInvitee]:
    """Interconsultas donde el médico autenticado es el INVITADO. Datos limitados: motivo, notas,
    edad del paciente y el video para unirse — SIN identidad del paciente (nombre/cédula/etc.).

    El invitado es equipo tratante de ese caso: motivo y notas en claro (`clinical_access =
    "full"`) si está habilitado para ejercer. Cada lectura queda en el audit_log."""
    return await interconsultations_service.list_for_invitee(db, principal, ip=client_ip(request))


@router.get(
    "/for-consultation/{consultation_id}",
    response_model=InterconsultationResponse | None,
    summary="Interconsulta de una consulta (para el médico que atiende)",
    responses={
        403: {"description": "No eres el médico asignado a esta consulta (ni admin)."},
        404: {"description": "Consulta no encontrada."},
    },
)
async def interconsultation_for_consultation(
    consultation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_staff),
) -> InterconsultationResponse | None:
    """La interconsulta activa de una consulta (o `null` si aún no tiene).

    Solo el médico asignado a la consulta (nota en claro) o un admin (nota en null,
    `clinical_access = "none"`). Otro médico recibe 403 y el intento queda auditado."""
    return await interconsultations_service.get_for_consultation(
        db, consultation_id, principal=principal, ip=client_ip(request)
    )
