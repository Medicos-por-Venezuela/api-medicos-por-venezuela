"""Pydantic schemas for affected_zones."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AffectedZoneStatus = Literal["active", "inactive", "deleted"]
EditableStatus = Literal["active", "inactive"]


class AffectedZoneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=120)
    state: str = Field(..., min_length=2, max_length=120)
    country: str | None = Field(default="Venezuela", min_length=2, max_length=120)
    status: EditableStatus = "active"


class AffectedZoneUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    state: str | None = Field(default=None, min_length=2, max_length=120)
    country: str | None = Field(default=None, min_length=2, max_length=120)
    status: EditableStatus | None = None

    @model_validator(mode="after")
    def reject_null_updates(self) -> "AffectedZoneUpdate":
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class AffectedZonePublicResponse(BaseModel):
    """Vista pública reducida de una zona afectada."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    state: str
    country: str | None


class AffectedZoneResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    state: str
    country: str | None
    status: AffectedZoneStatus
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
