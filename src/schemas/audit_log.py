"""Esquema Pydantic para leer el registro de auditoría."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_user_id: uuid.UUID | None = None
    action: str
    resource: str | None = None
    resource_id: str | None = None
    # Lee del atributo ORM `metadata_` y se expone como "metadata" en el JSON.
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    ip: str | None = None
    correlation_id: str | None = None
    created_at: datetime
