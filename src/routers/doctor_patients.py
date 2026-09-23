"""Capa HTTP de los pacientes de consultorio (alta por médico).

Segunda vía de alta de `patients`, para que un médico registre a un paciente suyo —que NO está
en la plataforma ni pasa por la cola— y pueda pedir una interconsulta sobre su caso. Ver
tasks/interconsulta-asincrona/spec.md.

Módulo aparte de `doctors.py` a propósito: es otro recurso (pacientes), no otra operación sobre
médicos. Además el auto-discovery ordena alfabéticamente y `doctor_patients` entra antes que
`doctors`, así que estas rutas nunca quedarían tapadas por `/doctors/{doctor_id}`.

El permiso `interconsultation_requests.write` autoriza la ACCIÓN; la pertenencia del paciente
concreto se valida en el servicio (regla IDOR de @.claude/rules/security.md).

`description`/`allergies` (cifrados) los lee el médico dueño si está habilitado para ejercer: es
el médico que trata a ese paciente. Cada lectura queda en `audit_log` (`READ_CLINICAL_DATA`).
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.observability import client_ip
from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.models.patient import Patient
from src.schemas.clinical import clinical_context
from src.schemas.patient import DoctorPatientCreate, DoctorPatientUpdate, PatientResponse
from src.services import clinical_access
from src.services import patients as patients_service

router = APIRouter(prefix="/doctors/me/patients", tags=["doctor-patients"])
tag_metadata = [
    {
        "name": "doctor-patients",
        "description": (
            "Pacientes de consultorio del médico autenticado: los que registra él mismo para "
            "pedir una interconsulta. No entran a la cola pública."
        ),
    }
]

_WRITE = "interconsultation_requests.write"


async def _responses(
    db: AsyncSession, request: Request, principal: Principal, patients: list[Patient]
) -> list[PatientResponse]:
    """El servicio ya garantizó que son suyos (`created_by_doctor_id == principal.id`): el
    dueño es su médico tratante. `treating_doctor_grant` exige además que ejerza, así que un
    admin que registró un paciente de consultorio no lee su contenido clínico."""
    pares = [
        (p, clinical_access.treating_doctor_grant(principal, p.created_by_doctor_id))
        for p in patients
    ]
    responses = [
        PatientResponse.model_validate(p, context=clinical_context(grant)) for p, grant in pares
    ]
    await clinical_access.audit_clinical_read(
        db,
        principal=principal,
        ip=client_ip(request),
        resource="patients",
        grants=[(p.id, grant) for p, grant in pares],
    )
    return responses


_AJENO = {
    403: {"description": "El paciente no fue registrado por quien llama (queda auditado)."},
    404: {"description": "Paciente no encontrado (o archivado)."},
}


@router.post(
    "",
    response_model=PatientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar un paciente de consultorio",
    responses={
        400: {"description": "Falta declarar el consentimiento (`consent = true`)."},
        422: {"description": "Payload inválido."},
    },
)
async def create_my_patient(
    payload: DoctorPatientCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> PatientResponse:
    """Da de alta un paciente propio, a nombre del médico autenticado.

    No pide teléfono ni zona afectada: este paciente no entra a la cola y nadie de la plataforma
    lo contacta. Sí exige `consent = true` — el médico declara que su paciente autorizó
    compartir el caso con un especialista."""
    patient = await patients_service.create_doctor_patient(db, payload, doctor_id=principal.id)
    return (await _responses(db, request, principal, [patient]))[0]


@router.get(
    "",
    response_model=list[PatientResponse],
    summary="Listar mis pacientes de consultorio",
)
async def list_my_patients(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> list[PatientResponse]:
    """Solo los que registró quien llama, más recientes primero. No incluye los archivados
    ni los pacientes de altas públicas."""
    patients = await patients_service.list_doctor_patients(
        db, doctor_id=principal.id, skip=skip, limit=limit
    )
    return await _responses(db, request, principal, patients)


@router.get(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="Obtener un paciente propio",
    responses=_AJENO,
)
async def get_my_patient(
    patient_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> PatientResponse:
    """403 si el paciente existe pero lo registró otro médico (auditado como intento
    denegado)."""
    patient = await patients_service.get_doctor_patient(
        db, patient_id, principal=principal, ip=client_ip(request)
    )
    return (await _responses(db, request, principal, [patient]))[0]


@router.patch(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="Actualizar un paciente propio",
    responses={**_AJENO, 422: {"description": "Payload inválido."}},
)
async def update_my_patient(
    patient_id: uuid.UUID,
    payload: DoctorPatientUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> PatientResponse:
    """`consent` y el dueño no se editan por acá: se fijan en el alta y no cambian."""
    patient = await patients_service.update_doctor_patient(
        db, patient_id, payload, principal=principal, ip=client_ip(request)
    )
    return (await _responses(db, request, principal, [patient]))[0]


@router.delete(
    "/{patient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archivar un paciente propio (baja lógica)",
    responses=_AJENO,
)
async def delete_my_patient(
    patient_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission(_WRITE)),
) -> None:
    """Baja lógica (`deleted_at`), nunca borrado duro: mismo criterio que el resto de la tabla."""
    await patients_service.delete_doctor_patient(
        db, patient_id, principal=principal, ip=client_ip(request)
    )
