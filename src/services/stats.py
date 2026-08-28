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
from src.models.specialty import Specialty
from src.schemas.stats import PublicStatsResponse, StatsResponse
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


# --- Cifras públicas de la portada -------------------------------------------

# Escalones del redondeo a la baja. Los dos primeros los fijó el equipo: 379 consultas se publican
# como "+300" y 450 como "+400" (centenas), y 2.900 médicos como "+2.500" (medios millares). Los
# otros dos están para que la cifra siga significando algo cuando todavía es pequeña: sin ellos,
# 47 consultas se publicarían como "+0".
_ESCALONES = ((1000, 500), (100, 100), (10, 10))


def round_down(n: int) -> int:
    """Redondea a la baja al escalón que corresponda a la magnitud de `n`.

    Siempre hacia abajo, nunca al más cercano: la cifra que se publica tiene que ser una que la
    organización pueda defender ("hay AL MENOS estos"), y redondear hacia arriba convertiría un
    dato real en una exageración.
    """
    for minimo, escalon in _ESCALONES:
        if n >= minimo:
            return (n // escalon) * escalon
    return n


async def get_public_stats(session: AsyncSession) -> PublicStatsResponse:
    """Las tres cifras de la banda de impacto del home, ya redondeadas.

    Tres conteos y ninguna fila leída: solo `COUNT(*)`. Los criterios son los mismos que usa el
    panel admin, para que la portada y el panel no cuenten cosas distintas — salvo en consultas,
    donde aquí se cuentan TODAS las creadas (decisión del equipo, 2026-08-28), sin los buckets por
    estado del dashboard.
    """
    doctors = (
        await session.scalar(
            select(func.count())
            .select_from(Doctor)
            .where(Doctor.status == 1, Doctor.deleted_at.is_(None))
        )
    ) or 0
    consultations = await session.scalar(select(func.count()).select_from(Consultation)) or 0
    specialties = (
        await session.scalar(
            select(func.count())
            .select_from(Specialty)
            .where(Specialty.status == "active", Specialty.deleted_at.is_(None))
        )
    ) or 0

    return PublicStatsResponse(
        doctors=round_down(doctors),
        consultations=round_down(consultations),
        specialties=round_down(specialties),
    )
