"""Esquemas Pydantic para patients (Create / Update / Response)."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class PatientBase(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=200)
    phone_whatsapp: str = Field(..., min_length=5, max_length=30)
    affected_zone: str = Field(..., min_length=2, max_length=100)
    cedula: str | None = Field(default=None, max_length=20)
    age_range: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    needs_tags: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=20
    )
    description: str | None = Field(default=None, max_length=2000)
    user_id: uuid.UUID | None = None


class PatientCreate(PatientBase):
    model_config = ConfigDict(extra="forbid")

    # El insert exige consentimiento (ver política RLS patients_insert_public).
    consent: bool = True


class PatientUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2)
    phone_whatsapp: str | None = Field(default=None, min_length=5)
    affected_zone: str | None = Field(default=None, min_length=2)
    cedula: str | None = None
    age_range: str | None = None
    email: EmailStr | None = None
    needs_tags: list[str] | None = None
    description: str | None = None


class PatientResponse(PatientBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    consent: bool
    consent_at: datetime | None = None
    created_at: datetime
