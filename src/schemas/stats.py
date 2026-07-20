"""Esquema de respuesta para el dashboard de estadísticas admin."""

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["StatsResponse"]


class StatsResponse(BaseModel):
    """Contadores agregados del panel admin. Sustituye a las 7 consultas directas
    que el frontend hacía contra Supabase (ver spec `dashboard-stats`).

    Note: the 4 consultation buckets are NOT mutually exclusive and must not be
    summed expecting a partition. In particular, `consultations_urgent`
    (`urgent_in_person`) is also counted within `consultations_in_progress`.
    """

    model_config = ConfigDict(from_attributes=True)

    doctors_registered: int = Field(
        ...,
        description="Médicos activos: `doctors.status == 1`, no borrados (`deleted_at` nulo).",
    )
    doctors_online: int = Field(
        ...,
        description=(
            "De los médicos activos, cuántos tienen presencia reciente: cuenta ligada "
            "(`users.last_seen_at`) dentro de los últimos 3 minutos."
        ),
    )
    patients_registered: int = Field(..., description="Total de pacientes registrados.")
    consultations_waiting: int = Field(
        ...,
        description=(
            "Consultas en espera con `entered_call_at` fijado (paridad con el panel legacy)."
        ),
    )
    consultations_in_progress: int = Field(
        ...,
        description=(
            "Bucket amplio 'en progreso': in_progress, referred_to_specialist, "
            "urgent_in_person, patient_no_show, cancelled, contacted_whatsapp. "
            "Note: overlaps with `consultations_urgent` — buckets are not disjoint."
        ),
    )
    consultations_closed: int = Field(
        ..., description="Consultas cerradas: closed o closed_by_admin."
    )
    consultations_urgent: int = Field(
        ...,
        description=(
            "Consultas marcadas `urgent_in_person`. Also counted within "
            "`consultations_in_progress`; do not sum buckets expecting a partition."
        ),
    )
