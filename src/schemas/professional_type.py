"""Pydantic schemas for professional_types."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ActiveStatus = Literal["active", "inactive"]
ProfessionalTypeStatus = Literal["active", "inactive", "deleted"]


class ProfessionalTypeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=120)
    status: ActiveStatus = "active"


class ProfessionalTypeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    status: ProfessionalTypeStatus | None = None


class ProfessionalTypeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: ProfessionalTypeStatus
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
