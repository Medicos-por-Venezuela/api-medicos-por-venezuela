"""Esquemas Pydantic para la verificación SACS."""

from pydantic import BaseModel

# Motivos tipados de un fallo de verificación. Los comparten el SACS y la FPV: son los mismos
# tres estados que puede tener cualquier consulta a un registro oficial externo.
NO_ENCONTRADO = "NO_ENCONTRADO"  # el registro respondió y la cédula no está
SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"  # no respondió, o respondió algo ilegible
FORMATO_INVALIDO = "FORMATO_INVALIDO"  # ni siquiera se llegó a consultar


class SacsVerificationResponse(BaseModel):
    encontrado: bool
    es_medico: bool | None = None
    nombre: str | None = None
    apellido: str | None = None
    profesion: str | None = None
    licencia: str | None = None
    error: str | None = None
    # Motivo TIPADO del fallo, junto al `error` en prosa. Existe porque hay lógica que necesita
    # distinguir "no está registrada" de "el servicio no respondió" (el correo al médico le dice
    # cosas distintas), y el único dato disponible era el mensaje en español. Ese mensaje es
    # texto para humanos: cambia con cualquier retoque de redacción, así que ramificar sobre él
    # rompería en silencio. Valores: NO_ENCONTRADO / SERVICIO_NO_DISPONIBLE / FORMATO_INVALIDO.
    error_kind: str | None = None
