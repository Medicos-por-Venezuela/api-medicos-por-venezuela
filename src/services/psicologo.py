"""Verificación de psicólogos contra la Federación de Psicólogos de Venezuela (FPV).

La FPV expone un endpoint JSON público (sistema.fpv.org.ve). A diferencia del SACS,
devuelve datos estructurados: `data.items` con los registros encontrados.
"""

import logging

import httpx

from src.schemas.psicologo import PsicologoVerificationResponse

logger = logging.getLogger("mpv.api")

_FPV_URL = "https://api.sistema.fpv.org.ve/api/v1/psicologos_public"


async def verificar_psicologo(cedula: str) -> PsicologoVerificationResponse:
    """Consulta la FPV y retorna si la cédula corresponde a un psicólogo colegiado."""
    # La API espera la cédula sin prefijo (solo dígitos); normalizamos por robustez.
    cedula = cedula.upper().lstrip("VE-").strip()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(_FPV_URL, params={"cedula": cedula})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("FPV HTTP error status=%s", exc.response.status_code)
        return PsicologoVerificationResponse(
            encontrado=False, error=f"Error HTTP de la FPV: {exc.response.status_code}"
        )
    except httpx.RequestError as exc:
        logger.warning("FPV connection error type=%s", type(exc).__name__)
        return PsicologoVerificationResponse(
            encontrado=False, error="Error de conexión con la FPV"
        )

    try:
        payload = response.json()
    except ValueError:
        return PsicologoVerificationResponse(
            encontrado=False, error="Respuesta inesperada de la FPV"
        )

    data = payload.get("data") or {}
    items = data.get("items", []) if isinstance(data, dict) else []

    if not items:
        return PsicologoVerificationResponse(
            encontrado=False, error="La cédula no está registrada en la FPV"
        )

    item = items[0]
    return PsicologoVerificationResponse(
        encontrado=True,
        nombre=(item.get("primerNombre") or "").upper(),
        apellido=(item.get("primerApellido") or "").upper(),
        licencia=item.get("fpv") or "",
    )
