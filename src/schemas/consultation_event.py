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
