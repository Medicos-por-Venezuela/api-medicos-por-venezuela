"""Esquemas Pydantic de las encuestas de marketing: el envío público y los agregados del panel.

El listado y la exportación del panel no tienen esquema propio: devuelven el `ReportPreview`
genérico de los reportes (ver `src/schemas/report.py`), así el panel pinta las respuestas con la
misma tabla que ya usa para médicos y pacientes. Los totales de las pestañas y los gráficos sí:
son agregados, no filas.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# Slug de cada encuesta: el mismo en la URL pública (/encuesta/<slug>), en el endpoint y en la
# columna `survey`, para no mantener equivalencias entre capas.
SurveySlug = Literal["psicologos", "especialistas", "medicos-generales"]

# Código de una opción marcada (p. ej. 'atender_pacientes'). Aquí solo se acota la forma: QUÉ
# códigos acepta cada encuesta lo decide el servicio, porque depende de la encuesta, que llega por
# la ruta y no en el cuerpo.
OptionCode = Annotated[str, Field(min_length=1, max_length=40)]


class SurveyResponseCreate(BaseModel):
    """Lo que manda el formulario público.

    Todos los campos existen para las tres encuestas; los que una encuesta no pregunta (la zona
    horaria en médicos generales, el "rol más activo" en las otras dos) se descartan en el
    servicio en vez de guardarse a medias.
    """

    model_config = ConfigDict(extra="forbid")

    # Llega en el enlace del correo masivo: se valida el formato, no que sea de quien responde.
    email: EmailStr
    roles: list[OptionCode] = Field(..., min_length=1, max_length=10)
    role_active_detail: str | None = Field(default=None, max_length=500)
    role_other_detail: str | None = Field(default=None, max_length=500)
    moments: list[OptionCode] = Field(default_factory=list, max_length=10)
    days: list[OptionCode] = Field(default_factory=list, max_length=10)
    weekly_hours: OptionCode | None = None
    availability_notes: str | None = Field(default=None, max_length=2000)
    timezone: OptionCode | None = None
    timezone_other: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    # Honeypot anti-bot, el mismo que el registro de médicos (`DoctorCreate.website`): el
    # formulario lo renderiza oculto y un humano no lo llena. Con valor, se rechaza el envío.
    website: str | None = Field(default=None, max_length=200)


class SurveyResponseReceipt(BaseModel):
    """Acuse del envío.

    No devuelve las respuestas: el endpoint es público y el correo no está verificado, así que
    devolverlas sería enseñarle a cualquiera lo que contestó otra persona con solo escribir su
    correo. `created_at` distinto de `updated_at` significa que se actualizó una respuesta previa.
    """

    survey: SurveySlug
    created_at: datetime
    updated_at: datetime


class SurveyTotalResponse(BaseModel):
    """Respuestas de una encuesta, para el número de su pestaña."""

    survey: SurveySlug
    total: int = Field(description="Respuestas guardadas (una por persona), sin filtros.")


class SurveyOptionCount(BaseModel):
    """Una opción de una pregunta y cuántas respuestas la marcaron."""

    code: str = Field(description="Código de la opción (p. ej. 'atender_pacientes').")
    label: str = Field(description="Texto que vio quien respondió, el de ESTA encuesta.")
    count: int = Field(description="Respuestas que marcaron la opción.")


class SurveyStatsResponse(BaseModel):
    """Agregados de una encuesta para los gráficos del panel.

    Las listas traen TODAS las opciones de cada pregunta en el orden del formulario, también las
    que tienen 0. En las preguntas de varias opciones (formas de participar, días, momentos) una
    respuesta cuenta en cada opción que marcó, así que los conteos pueden sumar más que `total`.
    """

    survey: SurveySlug
    total: int = Field(description="Respuestas que cumplen los filtros.")
    filters: list[list[str]] = Field(
        default_factory=list,
        description="Filtros aplicados, legibles, como pares [etiqueta, valor].",
    )
    roles: list[SurveyOptionCount] = Field(description="Cómo quieren participar.")
    days: list[SurveyOptionCount] = Field(description="Días de la semana.")
    moments: list[SurveyOptionCount] = Field(description="Momento del día.")
    availability: list[list[int]] = Field(
        description=(
            "Matriz días × momentos: `availability[i][j]` son las respuestas que marcaron el "
            "día `days[i]` y el momento `moments[j]`. Las dos preguntas son independientes, así "
            "que es cobertura posible, no un horario pactado."
        )
    )
    weekly_hours: list[SurveyOptionCount] = Field(description="Horas a la semana.")
    min_weekly_hours: int = Field(
        description=(
            "Horas semanales que, como mínimo, ofrecen entre todos: la suma del piso de cada "
            "rango (menos de 1 hora cuenta 0; más de 6, 6)."
        )
    )
    timezones: list[SurveyOptionCount] | None = Field(
        description="Dónde están. `null` en la encuesta que no lo pregunta (médicos generales)."
    )


class MarketingCampaignResponse(BaseModel):
    """Un envío de Kit asociado a una encuesta."""

    id: int = Field(description="Id del envío (broadcast) en Kit.")
    subject: str
    sent_at: datetime
    recipients: int = Field(description="Destinatarios del envío.")
    opened: int = Field(description="Personas que lo abrieron.")
    clicked: int = Field(description="Personas que hicieron clic (no clics totales).")
    unsubscribed: int = Field(description="Bajas desde este envío.")


class SurveyPerformanceResponse(BaseModel):
    """El embudo de una encuesta: de sus envíos de Kit a sus respuestas."""

    survey: SurveySlug
    campaigns: list[MarketingCampaignResponse] = Field(
        description="Envíos de Kit asociados a la encuesta, del más antiguo al más reciente."
    )
    recipients: int | None = Field(
        description=(
            "Suma de los destinatarios de sus envíos. `null` si no hay datos de Kit o la "
            "encuesta no tiene envíos: no es lo mismo que 0."
        )
    )
    opened: int | None
    clicked: int | None
    unsubscribed: int | None
    responses: int = Field(description="Todas las respuestas guardadas de la encuesta.")
    responses_after_send: int | None = Field(
        description=(
            "Respuestas llegadas desde el primer envío: el numerador de la tasa de respuesta. "
            "`null` si la encuesta no tiene envíos."
        )
    )
    first_sent_at: datetime | None


class ResponseTimelineResponse(BaseModel):
    """Respuestas nuevas por tramo de tiempo, desde el primer envío."""

    granularity: Literal["hour", "day"] = Field(
        description="Por horas hasta 72 h después del primer envío; después, por días."
    )
    buckets: list[datetime] = Field(
        description="Inicio de cada tramo (los días van de medianoche a medianoche en Caracas)."
    )
    responses: dict[SurveySlug, list[int]] = Field(
        description="Por encuesta, respuestas nuevas en cada tramo (misma longitud que `buckets`)."
    )


class MarketingPerformanceResponse(BaseModel):
    """Rendimiento de la campaña: métricas de Kit + respuestas de la plataforma."""

    kit_status: Literal["ok", "not_configured", "unavailable"] = Field(
        description=(
            "`not_configured`: falta `KIT_API_KEY`. `unavailable`: Kit falló o rechazó la clave. "
            "En los dos casos las métricas de Kit salen en `null` y el resto sigue."
        )
    )
    kit_fetched_at: datetime | None = Field(
        description="Cuándo se pidieron a Kit las métricas (se reutilizan unos minutos)."
    )
    surveys: list[SurveyPerformanceResponse]
    timeline: ResponseTimelineResponse
