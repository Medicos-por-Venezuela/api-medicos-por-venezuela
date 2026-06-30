"""Esquemas Pydantic para doctors."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DoctorBase(BaseModel):
    full_name: str
    specialty: str
    country: str
    phone_whatsapp: str
    preferred_platform: str = "google_meet"
    status: str = "active"
    cmv_number: str | None = None
    msds_number: str | None = None
    sanidad_number: str | None = None
    colegio_number: str | None = None
    foreign_registration: dict | None = None
    email: str | None = None
    phone: str | None = None
    user_id: uuid.UUID | None = None


class DoctorCreate(DoctorBase):
    pass


class DoctorUpdate(BaseModel):
    full_name: str | None = None
    specialty: str | None = None
    country: str | None = None
    phone_whatsapp: str | None = None
    preferred_platform: str | None = None
    status: str | None = None
    cmv_number: str | None = None
    msds_number: str | None = None
    sanidad_number: str | None = None
    colegio_number: str | None = None
    foreign_registration: dict | None = None
    email: str | None = None
    phone: str | None = None


class DoctorRead(DoctorBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
