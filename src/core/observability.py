"""Observabilidad y cabeceras de seguridad HTTP.

Middlewares:
- `CorrelationIdMiddleware`: asigna X-Correlation-ID a cada request y lo propaga.
- `SecurityHeadersMiddleware`: añade cabeceras de seguridad HTTP (OWASP A05).

Logging: JSON estructurado con correlation_id inyectado en cada registro.
Prohibido `print`: usar el logger `mpv.api`.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

CORRELATION_HEADER = "X-Correlation-ID"
correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="-")

logger = logging.getLogger("mpv.api")


class _CorrelationFilter(logging.Filter):
    """Inyecta el correlation_id actual en cada LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    """Formatea los logs como una línea JSON (apto para agregadores)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Configura el logger de la app con formato JSON y el filtro de correlation id."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_CorrelationFilter())

    app_logger = logging.getLogger("mpv.api")
    app_logger.handlers = [handler]
    app_logger.setLevel(level)
    app_logger.propagate = False


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Asigna un Correlation-ID a cada petición y lo devuelve en la respuesta."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex
        token = correlation_id_ctx.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            correlation_id_ctx.reset(token)
        response.headers[CORRELATION_HEADER] = correlation_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Añade cabeceras de seguridad HTTP recomendadas por OWASP (A05).

    - X-Content-Type-Options: evita MIME-sniffing (XSS via recursos).
    - X-Frame-Options: bloquea clickjacking en iframes.
    - Referrer-Policy: no filtra la URL de origen en requests cross-origin.
    - Strict-Transport-Security: fuerza HTTPS solo en producción.
    """

    def __init__(self, app: object, is_production: bool = False) -> None:
        super().__init__(app)
        self._is_production = is_production

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if self._is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response
