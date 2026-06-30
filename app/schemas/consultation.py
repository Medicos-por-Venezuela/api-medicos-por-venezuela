"""Esquemas Pydantic para consultations."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

# Re-exportado desde el modelo para tener una única fuente de verdad.
from app.models.consultation import CONSULTATION_STATUSES

__all__ = [
    "CONSULTATION_STATUSES",
    "ConsultationCreate",
    "ConsultationUpdate",
    "ConsultationRead",
]


class ConsultationBase(BaseModel):
    patient_id: uuid.UUID
    priority: str = "normal"
    category: str | None = None
    chief_complaint: str | None = None
    referred_specialty: str | None = None
    doctor_id: uuid.UUID | None = None
    assigned_doctor_id: uuid.UUID | None = None
    platform_used: str | None = None
    meeting_link: str | None = None
    video_room_url: str | None = None


class ConsultationCreate(ConsultationBase):
    # code es opcional: si se omite, el trigger generate_consultation_code lo asigna.
    code: str | None = None
    status: str = "waiting"


class ConsultationUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    category: str | None = None
    chief_complaint: str | None = None
    clinical_notes: str | None = None
    internal_note: str | None = None
    doctor_id: uuid.UUID | None = None
    assigned_doctor_id: uuid.UUID | None = None
    referred_specialty: str | None = None
    platform_used: str | None = None
    meeting_link: str | None = None
    video_room_url: str | None = None
    contacted: bool | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None


class ConsultationRead(ConsultationBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: str
    clinical_notes: str | None = None
    internal_note: str | None = None
    doctor_license_snapshot: dict | None = None
    has_prescription: bool
    has_referral: bool
    has_rest_note: bool
    follow_up_scheduled: bool
    contacted: bool
    queued_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    patient_last_seen_at: datetime | None = None
    created_at: datetime
