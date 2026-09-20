"""Capa de negocio para el dashboard de estadísticas admin.

Calcula los 11 KPIs + 2 distribuciones en 5 consultas de solo-conteo (round-trips),
reemplazando las 7 consultas directas a Supabase que hacía el frontend. Reutiliza
`ONLINE_WINDOW` de `services/doctors.py` (única fuente de verdad del criterio "online" = 3 min).
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consultation import Consultation
from src.models.doctor import Doctor
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.schemas.stats import (
    PublicStatsResponse,
    SpecialtyCount,
    StatsResponse,
    ZoneCount,
)
from src.services.doctors import ONLINE_WINDOW

# Universo de "médico": cuentas con rol clínico (mismo criterio que la página "Médicos y
# administradores" y el credential-summary). Antes se contaban fichas de `doctors`, lo que
# dejaba fuera cuentas sin ficha y no cuadraba con el resto del panel.
DOCTOR_ROLES = ("doctor", "specialist")
# Buckets MUTUAMENTE EXCLUYENTES por estado: cada consulta cae en exactamente uno. Antes el KPI
# "en progreso" era un bucket amplio que incluía derivadas, no-show, urgentes y contactados.
CLOSED_STATUSES = {"closed", "closed_by_admin"}
ATTENDING_STATUSES = {"in_progress", "contacted_whatsapp"}
_SIN_ZONA = "Sin zona"
_SIN_ESPECIALIDAD = "Sin especialidad"


async def get_dashboard_stats(session: AsyncSession) -> StatsResponse:
    """Calcula los KPIs del panel admin y las distribuciones por zona/especialidad.

    Cinco consultas de agregación (ninguna lee filas): una para médicos, una para pacientes,
    una para los buckets de consultas y una por cada distribución de los gráficos.
    """
    threshold = datetime.now(UTC) - ONLINE_WINDOW

    # 1) Médicos: cuentas con rol clínico; online = de esas, con presencia reciente
    # (users.last_seen_at) dentro de la ventana. Agregación condicional en una sola consulta.
    doctors_row = (
        await session.execute(
            select(
                func.count().label("registered"),
                func.count().filter(Profile.last_seen_at >= threshold).label("online"),
            )
            .select_from(Profile)
            .where(Profile.role.in_(DOCTOR_ROLES))
        )
    ).one()

    # 2) Pacientes: fichas vivas (soft-delete excluido). Sin filtro por cuenta: la mayoría de
    # pacientes son anónimos y solo existen como ficha.
    patients_registered = (
        await session.scalar(
            select(func.count()).select_from(Patient).where(Patient.deleted_at.is_(None))
        )
    ) or 0

    # 3) Consultas: un bucket por estado, en una sola consulta con agregación condicional.
    consultations_row = (
        await session.execute(
            select(
                func.count().filter(Consultation.status == "waiting").label("waiting"),
                func.count()
                .filter(Consultation.status.in_(ATTENDING_STATUSES))
                .label("in_progress"),
                func.count().filter(Consultation.status == "scheduled").label("scheduled"),
                func.count()
                .filter(Consultation.status == "referred_to_specialist")
                .label("referred"),
                func.count().filter(Consultation.status == "patient_no_show").label("no_show"),
                func.count().filter(Consultation.status == "cancelled").label("cancelled"),
                func.count().filter(Consultation.status.in_(CLOSED_STATUSES)).label("closed"),
                func.count().filter(Consultation.status == "urgent_in_person").label("urgent"),
            ).select_from(Consultation)
        )
    ).one()

    # 4) De qué zona llegan las consultas (todas, sin importar el estado). La zona vive en la
    # ficha del paciente como texto libre; se agrupa tal cual y los vacíos van a "Sin zona".
    zone_expr = func.coalesce(func.nullif(func.trim(Patient.affected_zone), ""), _SIN_ZONA)
    zone_rows = (
        await session.execute(
            select(zone_expr.label("zone"), func.count().label("total"))
            .select_from(Consultation)
            .join(Patient, Patient.id == Consultation.patient_id)
            .group_by(zone_expr)
            .order_by(func.count().desc(), zone_expr.asc())
        )
    ).all()

    # 5) Especialidad más pedida (todas, sin importar el estado). `specialty_id` es la columna
    # del matching; NULL (consultas viejas) cae en "Sin especialidad".
    specialty_expr = func.coalesce(Specialty.name, _SIN_ESPECIALIDAD)
    specialty_rows = (
        await session.execute(
            select(specialty_expr.label("specialty"), func.count().label("total"))
            .select_from(Consultation)
            .outerjoin(Specialty, Specialty.id == Consultation.specialty_id)
            .group_by(specialty_expr)
            .order_by(func.count().desc(), specialty_expr.asc())
        )
    ).all()

    return StatsResponse(
        doctors_registered=doctors_row.registered,
        doctors_online=doctors_row.online,
        patients_registered=patients_registered,
        consultations_waiting=consultations_row.waiting,
        consultations_in_progress=consultations_row.in_progress,
        consultations_scheduled=consultations_row.scheduled,
        consultations_referred=consultations_row.referred,
        consultations_no_show=consultations_row.no_show,
        consultations_cancelled=consultations_row.cancelled,
        consultations_closed=consultations_row.closed,
        consultations_urgent=consultations_row.urgent,
        consultations_by_zone=[ZoneCount(zone=r.zone, total=r.total) for r in zone_rows],
        consultations_by_specialty=[
            SpecialtyCount(specialty=r.specialty, total=r.total) for r in specialty_rows
        ],
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

    Tres conteos y ninguna fila leída: solo `COUNT(*)`. Se conservan los criterios históricos de la
    portada: médicos = fichas activas (`doctors.status == 1`, no borradas), no cuentas de usuario
    como el KPI del panel; y consultas = TODAS las creadas (decisión del equipo, 2026-08-28), sin
    los buckets por estado del dashboard. El número publicado va redondeado a la baja, así que el
    orden de magnitud no cambia por contar fichas en vez de cuentas.
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
