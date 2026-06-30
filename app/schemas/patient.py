"""Esquemas Pydantic para patients."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PatientBase(BaseModel):
    full_name: str
    phone_whatsapp: str
    affected_zone: str
    cedula: str | None = None
    age_range: str | None = None
    email: str | None = None
    needs_tags: list[str] = Field(default_factory=list)
    description: str | None = None
    user_id: uuid.UUID | None = None


class PatientCreate(PatientBase):
    # El insert exige consentimiento (ver política RLS patients_insert_public).
    consent: bool = True


class PatientUpdate(BaseModel):
    full_name: str | None = None
    phone_whatsapp: str | None = None
    affected_zone: str | None = None
    cedula: str | None = None
    age_range: str | None = None
    email: str | None = None
    needs_tags: list[str] | None = None
    description: str | None = None


class PatientRead(PatientBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    consent: bool
    consent_at: datetime | None = None
    created_at: datetime
