"""Esquemas Pydantic para la verificación de psicólogos en la FPV."""

from pydantic import BaseModel


class PsicologoVerificationResponse(BaseModel):
    encontrado: bool
    nombre: str | None = None
    apellido: str | None = None
    licencia: str | None = None
    error: str | None = None
