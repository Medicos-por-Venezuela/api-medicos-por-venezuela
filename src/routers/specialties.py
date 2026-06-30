"""Catálogo de especialidades y necesidades (solo lectura, sin base de datos)."""

from fastapi import APIRouter

from src.schemas.specialty import SpecialtyCatalogResponse
from src.services import specialties as specialties_service

router = APIRouter(prefix="/specialties", tags=["specialties"])


@router.get(
    "",
    response_model=SpecialtyCatalogResponse,
    summary="Catálogo de especialidades, necesidades y reglas de matching",
)
async def get_catalog() -> SpecialtyCatalogResponse:
    """Devuelve las mismas listas y reglas (`SPECIALTY_NEEDS`, `RESERVED_NEEDS`) que usa
    el frontend, para mantener una única fuente de verdad del matching."""
    return SpecialtyCatalogResponse(
        specialties=specialties_service.SPECIALTIES,
        needs=specialties_service.NEEDS,
        specialty_needs=specialties_service.SPECIALTY_NEEDS,
        reserved_needs=specialties_service.RESERVED_NEEDS,
    )
