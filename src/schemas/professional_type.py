"""Pydantic schemas for professional_types."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ActiveStatus = Literal["active", "inactive"]
ProfessionalTypeStatus = Literal["active", "inactive", "deleted"]


class ProfessionalTypeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=120)
    status: ActiveStatus = "active"


class ProfessionalTypeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    status: ActiveStatus | None = None

    @model_validator(mode="after")
    def reject_null_updates(self) -> "ProfessionalTypeUpdate":
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProfessionalTypeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: ProfessionalTypeStatus
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
