"""Esquemas de la agenda (sincronización de calendario)."""

from pydantic import BaseModel


class CalendarUrlResponse(BaseModel):
    """URL de suscripción del feed iCal del usuario. `webcal_url` abre el diálogo 'Agregar
    calendario' del SO/navegador; `ics_url` (https) sirve para copiar/pegar en Google Calendar."""

    ics_url: str
    webcal_url: str
