"""Esquemas Pydantic para consultation_events (Create / Response)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConsultationEventCreate(BaseModel):
    """Entrada del cliente: `created_by` se omite deliberadamente (siempre del JWT)."""

    model_config = ConfigDict(extra="forbid")

    consultation_id: uuid.UUID
    event_type: str = Field(..., min_length=2, max_length=50)
    note: str | None = Field(default=None, max_length=2000)


class ConsultationEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    consultation_id: uuid.UUID
    event_type: str
    note: str | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime
    # Autor resuelto server-side (join con users): evita que el frontend lea `users` directo para
    # pintar el nombre/rol de quien generó el evento. Nulos si el evento no tiene autor.
    author_name: str | None = None
    author_role: str | None = None
