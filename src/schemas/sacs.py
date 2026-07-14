"""Esquemas Pydantic para la verificación SACS."""

from pydantic import BaseModel


class SacsVerificationResponse(BaseModel):
    encontrado: bool
    es_medico: bool | None = None
    nombre: str | None = None
    apellido: str | None = None
    profesion: str | None = None
    licencia: str | None = None
    error: str | None = None
