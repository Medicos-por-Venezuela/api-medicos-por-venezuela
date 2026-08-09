"""Verificación de profesionales de salud contra el SACS (sacs.gob.ve)."""

import html
import json
import logging
import re
import time

import httpx

from src.schemas.sacs import SacsVerificationResponse

logger = logging.getLogger("mpv.api")

_SACS_URL = "https://sistemas.sacs.gob.ve/consultas/prfsnal_salud"
_CEDULA_RE = re.compile(r"^[VE]-\d+$")
_USER_RE = re.compile(r"xajax_userTable\('(.*?)'\)", re.DOTALL)
_PROF_RE = re.compile(r"xajax_tableProfesion\('(.*?)'\)", re.DOTALL)
_EMPTY_VALUES = {'""', "[]", ""}


def _fallo(error: str) -> SacsVerificationResponse:
    """Respuesta de "no verificado" con el motivo. Fail-closed: todo camino que no confirma
    la cédula pasa por aquí."""
    return SacsVerificationResponse(encontrado=False, error=error)


async def verificar_sacs(cedula: str) -> SacsVerificationResponse:
    """Consulta el SACS y retorna si la cédula corresponde a un profesional de salud."""
    cedula = cedula.upper().strip()

    if not _CEDULA_RE.match(cedula):
        return _fallo("Formato inválido. Usa V-12345678 o E-12345678")

    timestamp = int(time.time() * 1000)
    payload = f"xajax=getPrfsnalByCed&xajaxr={timestamp}&xajaxargs[]={cedula}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                _SACS_URL,
                content=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                },
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "SACS HTTP error status=%s cedula_prefix=%s", exc.response.status_code, cedula[:2]
        )
        return _fallo(f"Error HTTP del SACS: {exc.response.status_code}")
    except httpx.RequestError as exc:
        logger.warning("SACS connection error type=%s", type(exc).__name__)
        return _fallo("Error de conexión con el SACS")

    xml_text = response.text
    user_match = _USER_RE.search(xml_text)
    prof_match = _PROF_RE.search(xml_text)

    if not user_match or not prof_match:
        return _fallo("Respuesta inesperada del SACS")

    user_raw = user_match.group(1)
    prof_raw = prof_match.group(1)

    if user_raw in _EMPTY_VALUES or prof_raw in _EMPTY_VALUES:
        return _fallo("La cédula no está registrada en el SACS")

    try:
        user_data = json.loads(user_raw)
        profesion_data = json.loads(prof_raw)
    except json.JSONDecodeError:
        return _fallo("Error al parsear la respuesta del SACS")

    if not profesion_data:
        return SacsVerificationResponse(
            encontrado=True,
            es_medico=False,
            nombre=html.unescape(user_data.get("nombre1", "")),
            apellido=html.unescape(user_data.get("apellido1", "")),
            error="El usuario existe pero no tiene profesiones registradas",
        )

    prof_info = profesion_data[0]
    profesion_limpia = html.unescape(prof_info.get("profesion", "")).upper()
    es_medico = "MÉDICO" in profesion_limpia or "MEDICO" in profesion_limpia

    return SacsVerificationResponse(
        encontrado=True,
        es_medico=es_medico,
        nombre=html.unescape(user_data.get("nombre1", "")),
        apellido=html.unescape(user_data.get("apellido1", "")),
        profesion=profesion_limpia,
        licencia=prof_info.get("licencia", ""),
    )
