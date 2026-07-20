"""Esquemas Pydantic para consultations (Create / Update / Response)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Re-exportado desde el modelo para tener una única fuente de verdad.
from src.models.consultation import CONSULTATION_STATUSES

__all__ = [
    "CONSULTATION_STATUSES",
    "ConsultationCreate",
    "ConsultationUpdate",
    "ConsultationResponse",
    "ConsultationPatientResponse",
    "ConsultationCloseRequest",
    "QueueReleaseResponse",
]


class ConsultationBase(BaseModel):
    """Base usada SOLO por `ConsultationResponse` (esquema de salida).

    Sin `max_length`: son datos ya persistidos en la base y una validación de
    salida no debe rechazar filas existentes que excedan un límite pensado para
    entrada (ver ConsultationCreate/ConsultationUpdate, que sí lo validan).
    """

    patient_id: uuid.UUID
    priority: str = "normal"
    category: str | None = None
    # Especialidad solicitada por el paciente (catálogo specialties). Reemplaza a
    # needs_tags para el registro nuevo; el filtro del panel se actualiza aparte.
    specialty_id: uuid.UUID | None = None
    chief_complaint: str | None = None
    referred_specialty: str | None = None
    doctor_id: uuid.UUID | None = None
    assigned_doctor_id: uuid.UUID | None = None
    platform_used: str | None = None
    meeting_link: str | None = None
    video_room_url: str | None = None


class ConsultationCreate(BaseModel):
    """Entrada pública para crear una consulta.

    Solo acepta los campos que el cliente puede fijar legítimamente.
    Campos server-only excluidos deliberadamente: `assigned_doctor_id`, `doctor_id`,
    `video_room_url`, `meeting_link` (se asignan por el backend/cola, no por el cliente).
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: uuid.UUID
    priority: str = Field("normal", max_length=20)
    category: str | None = Field(default=None, max_length=100)
    specialty_id: uuid.UUID | None = None
    chief_complaint: str | None = Field(default=None, max_length=500)
    referred_specialty: str | None = Field(default=None, max_length=100)
    platform_used: str | None = Field(default=None, max_length=50)
    # El code lo asigna SIEMPRE el trigger generate_consultation_code en la base.
    status: str = "waiting"


class ConsultationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = Field(default=None, max_length=30)
    priority: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=100)
    specialty_id: uuid.UUID | None = None
    chief_complaint: str | None = Field(default=None, max_length=500)
    clinical_notes: str | None = Field(default=None, max_length=5000)
    internal_note: str | None = Field(default=None, max_length=2000)
    doctor_id: uuid.UUID | None = None
    assigned_doctor_id: uuid.UUID | None = None
    referred_specialty: str | None = Field(default=None, max_length=100)
    platform_used: str | None = Field(default=None, max_length=50)
    meeting_link: str | None = Field(default=None, max_length=500)
    video_room_url: str | None = Field(default=None, max_length=500)
    contacted: bool | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None


class ConsultationCloseRequest(BaseModel):
    """Cierre de consulta: `closed` (completada) o `patient_no_show` (ausencia).

    El autor del cierre se toma del JWT (no se acepta del cliente).
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["closed", "patient_no_show"] = "closed"
    note: str | None = Field(default=None, max_length=2000)


class QueueReleaseResponse(BaseModel):
    """Resultado de liberar consultas estancadas."""

    released: int
    threshold_minutes: int


class ConsultationResponse(ConsultationBase):
    """Vista completa para staff (incluye notas clínicas e internas)."""

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
    # Enriquecimiento para el panel admin (monitor de consultas): nombres resueltos
    # server-side vía join (patients.full_name / users.full_name por
    # assigned_doctor_id). Opcionales: nulos si el servicio no los resuelve o la
    # consulta está sin asignar.
    patient_name: str | None = None
    assigned_doctor_name: str | None = None


class ConsultationPatientResponse(BaseModel):
    """Vista reducida para pacientes autenticados.

    Excluye deliberadamente `internal_note`, `clinical_notes` y
    `doctor_license_snapshot`: son campos de uso interno del staff
    que no deben ser visibles al paciente (equivalente a la RLS de Supabase).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    patient_id: uuid.UUID
    status: str
    priority: str
    category: str | None = None
    specialty_id: uuid.UUID | None = None
    chief_complaint: str | None = None
    referred_specialty: str | None = None
    platform_used: str | None = None
    video_room_url: str | None = None
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
