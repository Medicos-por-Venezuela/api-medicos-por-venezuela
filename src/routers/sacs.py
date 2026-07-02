"""Capa HTTP para la verificación de profesionales en el SACS.

Endpoint público: el SACS es un registro gubernamental abierto; no requiere
autenticación. No realiza escrituras en BD.
"""

from typing import Annotated

from fastapi import APIRouter, Path

from src.schemas.sacs import SacsVerificationResponse
from src.services import sacs as sacs_service

router = APIRouter(prefix="/verificacion-sacs", tags=["sacs"])
tag_metadata = [
    {
        "name": "sacs",
        "description": (
            "Verificación de profesionales en el SACS "
            "(registro nacional de salud de Venezuela, sacs.gob.ve)."
        ),
    }
]


@router.get(
    "/{cedula}",
    response_model=SacsVerificationResponse,
    summary="Verificar profesional en el SACS (público)",
    responses={
        200: {
            "description": (
                "Resultado de la consulta. `encontrado=false` cuando la cédula no está "
                "registrada en el SACS."
            )
        },
        422: {"description": "Formato de cédula inválido. Debe comenzar con V- o E-."},
    },
)
async def verificar_sacs(
    cedula: Annotated[
        str,
        Path(pattern=r"^[VEve]-\d+$", description="Cédula venezolana: V-12345678 o E-12345678"),
    ],
) -> SacsVerificationResponse:
    """Consulta el SACS (sacs.gob.ve) para verificar si una cédula corresponde a un
    profesional de salud registrado. El campo `es_medico` indica si la profesión
    registrada contiene 'MÉDICO'. Acepta formato `V-12345678` o `E-12345678`."""
    return await sacs_service.verificar_sacs(cedula)
