"""Esquemas Pydantic para el catálogo de especialidades y necesidades."""

from pydantic import BaseModel


class SpecialtyCatalogResponse(BaseModel):
    """Catálogos y reglas de matching (para que el frontend muestre los mismos datos)."""

    specialties: list[str]
    needs: list[str]
    specialty_needs: dict[str, list[str]]
    reserved_needs: dict[str, list[str]]
