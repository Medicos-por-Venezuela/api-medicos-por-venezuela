"""Modelos ORM. Importarlos aquí registra las tablas en la metadata de Base."""

from app.models.admin_user import AdminUser
from app.models.clinical import (
    FollowUp,
    Message,
    Prescription,
    Referral,
    RestNote,
    TreatmentPlan,
)
from app.models.consultation import Consultation
from app.models.consultation_event import ConsultationEvent
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.profile import Profile

__all__ = [
    "Profile",
    "Patient",
    "Doctor",
    "Consultation",
    "ConsultationEvent",
    "Prescription",
    "Referral",
    "RestNote",
    "TreatmentPlan",
    "FollowUp",
    "Message",
    "AdminUser",
]
