"""Capa HTTP para la verificación de profesionales en el SACS.

Endpoint público: el SACS es un registro gubernamental abierto; no requiere
autenticación. No realiza escrituras en BD.
"""

from fastapi import APIRouter

from src.schemas.sacs import SacsVerificationResponse
from src.services import sacs as sacs_service

router = APIRouter(prefix="/verificacion-sacs", tags=["sacs"])


@router.get(
    "/{cedula}",
    response_model=SacsVerificationResponse,
    summary="Verificar profesional en el SACS (público)",
    responses={
        200: {
            "description": (
                "Resultado de la consulta. `encontrado=false` cuando la cédula no está "
                "registrada o el formato es inválido — nunca lanza 4xx por datos del SACS."
            )
        }
    },
)
async def verificar_sacs(cedula: str) -> SacsVerificationResponse:
    """Consulta el SACS (sacs.gob.ve) para verificar si una cédula corresponde a un
    profesional de salud registrado. El campo `es_medico` indica si la profesión
    registrada contiene 'MÉDICO'. Acepta formato `V-12345678` o `E-12345678`."""
    return await sacs_service.verificar_sacs(cedula)
