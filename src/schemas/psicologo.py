"""Esquemas Pydantic para la verificación de psicólogos en la FPV."""

from pydantic import BaseModel


class PsicologoVerificationResponse(BaseModel):
    encontrado: bool
    nombre: str | None = None
    apellido: str | None = None
    licencia: str | None = None
    error: str | None = None
    # Motivo TIPADO del fallo, junto al `error` en prosa. Existe porque hay lógica que necesita
    # distinguir "no está registrada" de "el servicio no respondió" (el correo al médico le dice
    # cosas distintas), y el único dato disponible era el mensaje en español. Ese mensaje es
    # texto para humanos: cambia con cualquier retoque de redacción, así que ramificar sobre él
    # rompería en silencio. Valores: NO_ENCONTRADO / SERVICIO_NO_DISPONIBLE / FORMATO_INVALIDO.
    error_kind: str | None = None
