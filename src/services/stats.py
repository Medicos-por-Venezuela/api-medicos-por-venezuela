"""Capa de negocio para el dashboard de estadísticas admin.

Calcula los 7 KPIs en 3 consultas de solo-conteo (round-trips), reemplazando las
7 consultas directas a Supabase que hacía el frontend. Reutiliza `ONLINE_WINDOW`
de `services/doctors.py` (única fuente de verdad del criterio "online" = 3 min).
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consultation import Consultation
from src.models.doctor import Doctor
from src.models.patient import Patient
from src.models.profile import Profile
from src.schemas.stats import StatsResponse
from src.services.doctors import ONLINE_WINDOW

# Bucket amplio "en progreso" (igual criterio que el KPI del panel legacy).
IN_PROGRESS_STATUSES = {
    "in_progress",
    "referred_to_specialist",
    "urgent_in_person",
    "patient_no_show",
    "cancelled",
    "contacted_whatsapp",
}
CLOSED_STATUSES = {"closed", "closed_by_admin"}


async def get_dashboard_stats(session: AsyncSession) -> StatsResponse:
    """Calcula los 7 contadores del dashboard admin."""
    threshold = datetime.now(UTC) - ONLINE_WINDOW

    # 1) Médicos: registrados = status=1 no borrados; online = de esos, con
    # presencia (users.last_seen_at) dentro de la ventana. Una sola consulta con
    # agregación condicional (func.count().filter -> SQL FILTER (WHERE ...)).
    doctors_row = (
        await session.execute(
            select(
                func.count().label("registered"),
                func.count().filter(Profile.last_seen_at >= threshold).label("online"),
            )
            .select_from(Doctor)
            .outerjoin(Profile, Doctor.user_id == Profile.id)
            .where(Doctor.status == 1, Doctor.deleted_at.is_(None))
        )
    ).one()

    # 2) Pacientes: total simple.
    patients_registered = await session.scalar(select(func.count()).select_from(Patient)) or 0

    # 3) Consultas: los 4 buckets en una sola consulta con agregación condicional.
    consultations_row = (
        await session.execute(
            select(
                func.count()
                .filter(
                    Consultation.status == "waiting",
                    Consultation.entered_call_at.isnot(None),
                )
                .label("waiting"),
                func.count()
                .filter(Consultation.status.in_(IN_PROGRESS_STATUSES))
                .label("in_progress"),
                func.count().filter(Consultation.status.in_(CLOSED_STATUSES)).label("closed"),
                func.count().filter(Consultation.status == "urgent_in_person").label("urgent"),
            ).select_from(Consultation)
        )
    ).one()

    return StatsResponse(
        doctors_registered=doctors_row.registered,
        doctors_online=doctors_row.online,
        patients_registered=patients_registered,
        consultations_waiting=consultations_row.waiting,
        consultations_in_progress=consultations_row.in_progress,
        consultations_closed=consultations_row.closed,
        consultations_urgent=consultations_row.urgent,
    )
