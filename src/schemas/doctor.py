"""Esquemas Pydantic para doctors (Create / Update / Response)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class DoctorBase(BaseModel):
    full_name: str = Field(..., min_length=2)
    specialty: str = Field(..., min_length=2)
    country: str = Field(..., min_length=2)
    phone_whatsapp: str = Field(..., min_length=5)
    preferred_platform: str = "google_meet"
    status: str = "active"
    cmv_number: str | None = None
    msds_number: str | None = None
    sanidad_number: str | None = None
    colegio_number: str | None = None
    foreign_registration: dict | None = None
    email: EmailStr | None = None
    phone: str | None = None
    user_id: uuid.UUID | None = None


class DoctorCreate(DoctorBase):
    model_config = ConfigDict(extra="forbid")


class DoctorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2)
    specialty: str | None = Field(default=None, min_length=2)
    country: str | None = Field(default=None, min_length=2)
    phone_whatsapp: str | None = Field(default=None, min_length=5)
    preferred_platform: str | None = None
    status: str | None = None
    cmv_number: str | None = None
    msds_number: str | None = None
    sanidad_number: str | None = None
    colegio_number: str | None = None
    foreign_registration: dict | None = None
    email: EmailStr | None = None
    phone: str | None = None


class DoctorResponse(DoctorBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
