"""Capa HTTP (delgada) para el dashboard de estadísticas admin."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.stats import StatsResponse
from src.services import stats as stats_service

router = APIRouter(prefix="/stats", tags=["stats"])
tag_metadata = [
    {
        "name": "stats",
        "description": "Contadores agregados para el dashboard de administración.",
    }
]


@router.get(
    "/dashboard",
    response_model=StatsResponse,
    summary="Contadores del dashboard admin (médicos, pacientes, consultas)",
    responses={403: {"description": "No tienes el permiso 'stats.read'."}},
)
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("stats.read")),
) -> StatsResponse:
    """Calcula, en 3 consultas de solo-conteo, los 7 KPIs del dashboard admin:
    médicos registrados/online, pacientes registrados, y consultas agrupadas por
    estado (en espera, en progreso, cerradas, urgentes). Reemplaza las 7 consultas
    directas a Supabase que hacía antes el frontend."""
    return await stats_service.get_dashboard_stats(db)
