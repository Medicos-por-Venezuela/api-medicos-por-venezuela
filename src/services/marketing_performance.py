"""Rendimiento de la campaña de las encuestas: del correo de Kit a la respuesta en la plataforma.

Junta dos fuentes que por separado no alcanzan para decidir. Kit sabe del envío: enviados,
aperturas, clics y bajas (ver `services/kit.py`). La plataforma sabe de las respuestas y de
cuándo llegaron.

- **Tasa de respuesta real.** `responses_after_send` cuenta solo las respuestas llegadas DESPUÉS
  del primer envío de su encuesta. Las anteriores son pruebas del equipo o gente que llegó por
  otro lado, y sumarlas inflaría justo la tasa con la que se va a decidir.
- **Varios envíos de una misma encuesta** (un recordatorio, p. ej.) se suman: son correos
  enviados, no personas distintas.
- **Sin Kit** (sin clave, o Kit caído), el panel sigue funcionando: las métricas de Kit salen en
  `None` y `kit_status` dice por qué.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.tz import VET
from src.models.marketing_survey_response import MarketingSurveyResponse
from src.services import kit
from src.services.marketing import SURVEYS, survey_totals

# Hasta 72 horas desde el primer envío, la línea de tiempo va por horas: es cuando llega casi
# todo y cuando se decide si mandar un recordatorio. Después, por días, con un tope de 180 días
# para que el gráfico no crezca sin límite.
HOURLY_SPAN = timedelta(hours=72)
MAX_DAILY_SPAN = timedelta(days=180)


@dataclass(frozen=True)
class SurveyPerformance:
    survey: str
    campaigns: list[kit.Broadcast]
    # Suma de los envíos de la encuesta. `None` si no hay datos de Kit o la encuesta no tiene
    # ningún envío asociado: "no lo sabemos" no es lo mismo que 0.
    recipients: int | None
    opened: int | None
    clicked: int | None
    unsubscribed: int | None
    responses: int
    responses_after_send: int | None
    first_sent_at: datetime | None


@dataclass(frozen=True)
class Timeline:
    granularity: str  # "hour" | "day"
    buckets: list[datetime]  # inicio de cada tramo, en UTC
    responses: dict[str, list[int]]  # respuestas nuevas (la primera de cada persona) por tramo


@dataclass(frozen=True)
class Performance:
    kit_status: str  # "ok" | "not_configured" | "unavailable"
    kit_fetched_at: datetime | None
    surveys: list[SurveyPerformance]
    timeline: Timeline


async def campaign_performance(session: AsyncSession, *, refresh: bool = False) -> Performance:
    broadcasts: list[kit.Broadcast] = []
    fetched_at = None
    try:
        snapshot = await kit.survey_broadcasts(refresh=refresh)
        broadcasts, fetched_at, kit_status = snapshot.broadcasts, snapshot.fetched_at, "ok"
    except kit.KitNotConfiguredError:
        kit_status = "not_configured"
    except kit.KitUnavailableError:
        kit_status = "unavailable"

    by_survey = {slug: [b for b in broadcasts if b.survey == slug] for slug in SURVEYS}
    first_sent = {slug: min(b.sent_at for b in bs) for slug, bs in by_survey.items() if bs}
    totals = {t.survey: t.total for t in await survey_totals(session)}
    after_send = await _responses_after(session, first_sent)

    surveys = []
    for slug, campaigns in by_survey.items():
        known = kit_status == "ok" and bool(campaigns)
        surveys.append(
            SurveyPerformance(
                survey=slug,
                campaigns=campaigns,
                recipients=sum(b.recipients for b in campaigns) if known else None,
                opened=sum(b.opened for b in campaigns) if known else None,
                clicked=sum(b.clicked for b in campaigns) if known else None,
                unsubscribed=sum(b.unsubscribed for b in campaigns) if known else None,
                responses=totals[slug],
                responses_after_send=after_send.get(slug, 0) if slug in first_sent else None,
                first_sent_at=first_sent.get(slug),
            )
        )

    return Performance(
        kit_status=kit_status,
        kit_fetched_at=fetched_at,
        surveys=surveys,
        timeline=await _timeline(session, min(first_sent.values(), default=None)),
    )


async def _responses_after(
    session: AsyncSession, first_sent: dict[str, datetime]
) -> dict[str, int]:
    """Respuestas de cada encuesta llegadas desde su primer envío, en una sola consulta."""
    if not first_sent:
        return {}
    rows = await session.execute(
        select(MarketingSurveyResponse.survey, func.count())
        .where(
            or_(
                *(
                    and_(
                        MarketingSurveyResponse.survey == slug,
                        MarketingSurveyResponse.created_at >= sent_at,
                    )
                    for slug, sent_at in first_sent.items()
                )
            )
        )
        .group_by(MarketingSurveyResponse.survey)
    )
    return {slug: n for slug, n in rows.tuples()}


async def _timeline(session: AsyncSession, first_sent: datetime | None) -> Timeline:
    """Respuestas nuevas por hora (o por día) desde el primer envío de cualquier encuesta.

    Sin envíos conocidos, arranca en la primera respuesta. Los tramos son de hora de Venezuela:
    un día va de medianoche a medianoche en Caracas, no en UTC.
    """
    now = datetime.now(UTC)
    start = first_sent or await session.scalar(
        select(func.min(MarketingSurveyResponse.created_at))
    )
    start = start or now
    granularity = "hour" if now - start <= HOURLY_SPAN else "day"
    if granularity == "day":
        start = max(start, now - MAX_DAILY_SPAN)
    first_bucket = _floor(start, granularity)
    step = timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
    buckets = []
    bucket = first_bucket
    while bucket <= now:
        buckets.append(bucket)
        bucket += step

    # El tramo se calcula en la base, en hora de Venezuela: `timezone('UTC', ...)` da la hora UTC
    # sin zona, y sumarle el desfase de VET la deja en hora local para truncarla.
    local = func.timezone("UTC", MarketingSurveyResponse.created_at) + literal(VET.utcoffset(None))
    # Con etiqueta, para que el GROUP BY la nombre en vez de repetir la expresión: repetida, sus
    # parámetros saldrían con otros marcadores y Postgres no la reconocería como la misma.
    tramo = func.date_trunc(granularity, local).label("tramo")
    rows = await session.execute(
        select(MarketingSurveyResponse.survey, tramo, func.count())
        .where(MarketingSurveyResponse.created_at >= first_bucket)
        .group_by(MarketingSurveyResponse.survey, tramo)
    )
    index = {b.astimezone(VET).replace(tzinfo=None): i for i, b in enumerate(buckets)}
    series = {slug: [0] * len(buckets) for slug in SURVEYS}
    for slug, local_bucket, n in rows.tuples():
        if slug in series and (i := index.get(local_bucket)) is not None:
            series[slug][i] = n
    return Timeline(granularity=granularity, buckets=buckets, responses=series)


def _floor(moment: datetime, granularity: str) -> datetime:
    """Inicio de la hora o del día (de Venezuela) que contiene `moment`, en UTC."""
    local = moment.astimezone(VET)
    local = local.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        local = local.replace(hour=0)
    return local.astimezone(UTC)
