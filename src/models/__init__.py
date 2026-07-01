"""Modelos ORM. Importarlos aquí registra las tablas en la metadata de Base."""

from src.models.admin_user import AdminUser
from src.models.clinical import (
    FollowUp,
    Message,
    Prescription,
    Referral,
    RestNote,
    TreatmentPlan,
)
from src.models.consultation import Consultation
from src.models.consultation_event import ConsultationEvent
from src.models.doctor import Doctor
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty

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
    "Specialty",
]
