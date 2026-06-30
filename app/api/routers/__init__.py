"""Routers de la API v1."""

from fastapi import APIRouter

from app.api.routers import consultations, doctors, patients, profiles

api_router = APIRouter()
api_router.include_router(patients.router)
api_router.include_router(consultations.router)
api_router.include_router(doctors.router)
api_router.include_router(profiles.router)

__all__ = ["api_router"]
