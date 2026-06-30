"""Routers de la API v1."""

from fastapi import APIRouter

from src.routers import (
    auth,
    consultations,
    doctors,
    patients,
    profiles,
    queue,
    specialties,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(queue.router)
api_router.include_router(consultations.router)
api_router.include_router(patients.router)
api_router.include_router(doctors.router)
api_router.include_router(profiles.router)
api_router.include_router(specialties.router)

__all__ = ["api_router"]
