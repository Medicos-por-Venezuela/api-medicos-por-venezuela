"""Excepciones de dominio que lanza la capa de servicios.

Son agnósticas de HTTP: los servicios no conocen FastAPI. Los manejadores
globales (ver core/exceptions.py) las traducen a respuestas HTTP.
"""


class NotFoundError(Exception):
    """El recurso solicitado no existe. -> HTTP 404."""


class ConflictError(Exception):
    """Conflicto de estado o de concurrencia. -> HTTP 409."""


class BadRequestError(ValueError):
    """Petición inválida por reglas de negocio. -> HTTP 400."""


class UnprocessableError(ValueError):
    """Datos sintácticamente válidos pero semánticamente inválidos. -> HTTP 422."""
