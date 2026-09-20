"""Esquema de respuesta para el dashboard de estadísticas admin."""

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["PublicStatsResponse", "SpecialtyCount", "StatsResponse", "ZoneCount"]


class ZoneCount(BaseModel):
    """Consultas agrupadas por la zona del paciente (todas, sin importar el estado)."""

    zone: str = Field(
        ..., description="Zona de la ficha del paciente, o 'Sin zona' si está vacía."
    )
    total: int = Field(..., description="Consultas creadas por pacientes de esa zona.")


class SpecialtyCount(BaseModel):
    """Consultas agrupadas por la especialidad solicitada (todas, sin importar el estado)."""

    specialty: str = Field(
        ..., description="Especialidad pedida, o 'Sin especialidad' si la consulta no la trae."
    )
    total: int = Field(..., description="Consultas que pidieron esa especialidad.")


class StatsResponse(BaseModel):
    """Contadores agregados del panel admin (KPIs + distribuciones de los gráficos).

    Los buckets de consultas son MUTUAMENTE EXCLUYENTES por estado: cada consulta cae en
    exactamente uno, así que pueden sumarse y su total es el de consultas creadas.
    """

    model_config = ConfigDict(from_attributes=True)

    doctors_registered: int = Field(
        ...,
        description="Médicos registrados: cuentas con rol clínico ('doctor'/'specialist').",
    )
    doctors_online: int = Field(
        ...,
        description=(
            "De esas cuentas, cuántas tienen presencia reciente (users.last_seen_at dentro "
            "de los últimos 3 minutos)."
        ),
    )
    patients_registered: int = Field(
        ..., description="Fichas de pacientes vivas (deleted_at nulo)."
    )
    consultations_waiting: int = Field(
        ..., description="Consultas en espera (status='waiting'), con o sin entered_call_at."
    )
    consultations_in_progress: int = Field(
        ...,
        description="Casos con médico encima: in_progress + contacted_whatsapp.",
    )
    consultations_scheduled: int = Field(
        ..., description="Citas de la Agenda aún no atendidas (status='scheduled')."
    )
    consultations_referred: int = Field(
        ..., description="Derivadas a otra especialidad (status='referred_to_specialist')."
    )
    consultations_no_show: int = Field(
        ..., description="El paciente no se presentó (status='patient_no_show')."
    )
    consultations_cancelled: int = Field(..., description="Canceladas (status='cancelled').")
    consultations_closed: int = Field(..., description="Cerradas: closed + closed_by_admin.")
    consultations_urgent: int = Field(
        ..., description="Deben ir a atención presencial urgente (status='urgent_in_person')."
    )
    consultations_by_zone: list[ZoneCount] = Field(
        ..., description="Distribución de TODAS las consultas por zona del paciente."
    )
    consultations_by_specialty: list[SpecialtyCount] = Field(
        ..., description="Distribución de TODAS las consultas por especialidad solicitada."
    )


class PublicStatsResponse(BaseModel):
    """Cifras para la portada pública. Van **redondeadas hacia abajo desde el servidor**, no
    exactas: son las tres de la banda de impacto del home, y ese es todo el uso que tienen.

    El redondeo se hace aquí y no en el navegador a propósito. Este endpoint no pide token, así
    que cualquiera puede leerlo; devolver el conteo exacto publicaría el pulso operativo de la
    organización (cuántos médicos tiene, a qué ritmo crece) a quien mire la pestaña de red. Con
    los múltiplos de abajo, la cifra es cierta —siempre igual o menor que la real— y no dice nada
    más que el orden de magnitud.
    """

    model_config = ConfigDict(from_attributes=True)

    doctors: int = Field(
        ...,
        description="Médicos activos, redondeado a la baja. Mismo criterio que el KPI admin.",
    )
    consultations: int = Field(
        ..., description="Consultas creadas, redondeado a la baja. Todas, sin filtrar por estado."
    )
    specialties: int = Field(
        ..., description="Especialidades activas del catálogo, redondeado a la baja."
    )
