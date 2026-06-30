"""Capa de acceso a datos (CRUD)."""

from app.crud import (
    consultation_events,
    consultations,
    doctors,
    patients,
    profiles,
)

__all__ = [
    "patients",
    "consultations",
    "doctors",
    "profiles",
    "consultation_events",
]
