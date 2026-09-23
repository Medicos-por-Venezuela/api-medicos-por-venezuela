"""Esquemas de Interconsultas (segunda opinión en tiempo real). Ver .knowledge/interconsultas.md.

Dos vistas distintas por seguridad:
- `InterconsultationResponse`: la ve el médico que ATIENDE (a quién invitó).
- `InterconsultationForInvitee`: la ve el médico INVITADO — SOLO motivo, notas y edad del paciente,
  el video para unirse, y nada de identidad (sin nombre/cédula/teléfono/zona).

Los campos clínicos (motivo, notas, nota de la invitación) salen cifrados de la BD y solo se
descifran con el permiso que concede el router (ver src/schemas/clinical.py).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.clinical import ClinicalAccessMixin, ClinicalNote, ClinicalSummary

__all__ = [
    "InterconsultationCreate",
    "InterconsultationResponse",
    "InterconsultationForInvitee",
]


class InterconsultationCreate(BaseModel):
    """El médico que atiende invita a un médico del pool a la interconsulta."""

    consultation_id: uuid.UUID
    invited_doctor_id: uuid.UUID
    note: str | None = Field(default=None, max_length=1000)


class InterconsultationResponse(ClinicalAccessMixin):
    """Vista del médico que ATIENDE: a quién invitó y el estado. La consulta sigue abierta."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    consultation_id: uuid.UUID
    invited_doctor_id: uuid.UUID
    # Nombre del médico invitado (colega, no el paciente): resuelto server-side para la UI.
    invited_doctor_name: str | None = None
    created_by_id: uuid.UUID
    status: str
    note: ClinicalNote = None
    created_at: datetime


class InterconsultationForInvitee(ClinicalAccessMixin):
    """Vista del médico INVITADO: SOLO lo clínicamente relevante, sin identidad del paciente.
    Único dato del paciente: la edad (`patient_age_range`)."""

    id: uuid.UUID
    consultation_id: uuid.UUID
    status: str
    note: ClinicalNote = None  # mensaje/razón del médico que invitó
    # Datos de la consulta que el invitado necesita para dar la segunda opinión.
    chief_complaint: ClinicalSummary = None  # motivo
    internal_note: ClinicalNote = None  # notas del médico
    clinical_notes: ClinicalNote = None  # notas clínicas
    # ÚNICO dato del paciente que se expone.
    patient_age_range: str | None = None  # edad
    # Para unirse a la misma videoconsulta que el médico que atiende.
    video_room_url: str | None = None
    created_at: datetime
