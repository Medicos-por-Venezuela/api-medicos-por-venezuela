"""Capa HTTP (delgada) para el dashboard de estadísticas admin."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.stats import PublicStatsResponse, StatsResponse
from src.services import stats as stats_service

router = APIRouter(prefix="/stats", tags=["stats"])
tag_metadata = [
    {
        "name": "stats",
        "description": (
            "Contadores agregados: el dashboard de administración y las tres cifras "
            "públicas de la portada."
        ),
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


@router.get(
    "/public",
    response_model=PublicStatsResponse,
    summary="Cifras públicas de la portada (médicos, consultas, especialidades)",
)
async def get_public_stats(db: AsyncSession = Depends(get_db)) -> PublicStatsResponse:
    """Las tres cifras de la banda de impacto del home. **No pide token**: es lo que se pinta en
    una página pública, y exigir uno obligaría a inventarse una credencial de portada.

    Lo que sí hace es no decir la verdad exacta: los conteos salen **redondeados a la baja desde
    el servidor** (ver `PublicStatsResponse`), así que el número fino nunca sale de la base. Sin
    eso, publicar este endpoint sería publicar el pulso operativo de la organización.
    """
    return await stats_service.get_public_stats(db)
