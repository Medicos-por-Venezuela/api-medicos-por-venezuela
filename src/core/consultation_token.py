"""Token de acceso a una consulta para el paciente ANÓNIMO (hallazgo M3).

El paciente llega a su sala por un link, sin cuenta. Hasta ahora la única credencial para
`video-room` y `entered-call` era el UUID de la consulta: un secreto **eterno**. El UUID en sí
es infalsificable (`gen_random_uuid()`, 122 bits), así que el riesgo nunca fue la fuerza bruta
sino la FUGA — la URL viaja por el historial del navegador, el header `Referer`, las vistas
previas de WhatsApp, los logs de proxies y cualquier captura compartida. Quien la obtuviera
entraba a la videoconsulta, aunque fuese meses después.

Esto lo cambia por un JWT firmado con `exp`: la misma URL, pero con fecha de caducidad.

Lo que NO arregla: el token sigue viajando en la URL, porque el paciente llega por link y no
hay dónde guardarlo antes del primer render. La mejora es acotar la ventana, no eliminar la
fuga. Cerrarla del todo es autenticación de Jitsi con salas moderadas (opción 3 del hallazgo),
que toca la infraestructura self-hosted.

Secreto propio (`CONSULTATION_TOKEN_SECRET`), NO el de Supabase: si se firmara con el mismo,
un token de sala y un token de sesión serían indistinguibles para quien tuviera el secreto, y
un bug de verificación en un lado podría abrir el otro.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from src.core.config import settings

# Distingue estos tokens de cualquier otro JWT del sistema. Se verifica explícitamente:
# sin esto, un token de Supabase con el mismo secreto pasaría por token de sala.
_TOKEN_TYPE = "consultation_access"
_ALGORITHM = "HS256"


def issue(consultation_id: uuid.UUID) -> str:
    """Emite el token de acceso de una consulta. Se devuelve UNA vez, al crearla."""
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(consultation_id),
            "typ": _TOKEN_TYPE,
            "iat": now,
            "exp": now + timedelta(hours=settings.CONSULTATION_TOKEN_TTL_HOURS),
        },
        settings.CONSULTATION_TOKEN_SECRET,
        algorithm=_ALGORITHM,
    )


def is_valid_for(token: str | None, consultation_id: uuid.UUID) -> bool:
    """True si `token` es un token de sala vigente para ESA consulta.

    Comprueba el `sub` contra la consulta pedida: sin eso, un token válido de la consulta
    propia serviría para abrir la sala de cualquier otra (IDOR con credencial legítima).
    """
    if not token:
        return False
    try:
        payload = jwt.decode(token, settings.CONSULTATION_TOKEN_SECRET, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return False
    return payload.get("typ") == _TOKEN_TYPE and payload.get("sub") == str(consultation_id)
