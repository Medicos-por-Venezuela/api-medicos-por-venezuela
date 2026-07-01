"""Capa HTTP para la verificación de psicólogos en la FPV.

Endpoint público: la FPV es un registro gremial abierto; no requiere
autenticación. No realiza escrituras en BD.
"""

from typing import Annotated

from fastapi import APIRouter, Path

from src.schemas.psicologo import PsicologoVerificationResponse
from src.services import psicologo as psicologo_service

router = APIRouter(prefix="/verificacion-psicologo", tags=["psicologos"])
tag_metadata = [
    {
        "name": "psicologos",
        "description": (
            "Verificación de psicólogos en la FPV "
            "(Federación de Psicólogos de Venezuela, sistema.fpv.org.ve)."
        ),
    }
]


@router.get(
    "/{cedula}",
    response_model=PsicologoVerificationResponse,
    summary="Verificar psicólogo en la FPV (público)",
    responses={
        200: {
            "description": (
                "Resultado de la consulta. `encontrado=false` cuando la cédula no está "
                "registrada en la FPV."
            )
        },
        422: {
            "description": "Formato de cédula inválido (solo dígitos, con prefijo V-/E- opcional)."
        },
    },
)
async def verificar_psicologo(
    cedula: Annotated[
        str,
        Path(
            pattern=r"^[VEve]?-?\d{6,9}$",
            description="Cédula venezolana: 21560752 (prefijo V-/E- opcional)",
        ),
    ],
) -> PsicologoVerificationResponse:
    """Consulta la FPV (sistema.fpv.org.ve) para verificar si una cédula corresponde a
    un psicólogo colegiado. Devuelve nombre, apellido y número de licencia (`fpv`)."""
    return await psicologo_service.verificar_psicologo(cedula)
