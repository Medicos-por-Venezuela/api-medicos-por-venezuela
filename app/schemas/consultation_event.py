"""Esquemas Pydantic para consultation_events."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ConsultationEventBase(BaseModel):
    consultation_id: uuid.UUID
    event_type: str
    note: str | None = None
    created_by: uuid.UUID | None = None


class ConsultationEventCreate(ConsultationEventBase):
    pass


class ConsultationEventRead(ConsultationEventBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
