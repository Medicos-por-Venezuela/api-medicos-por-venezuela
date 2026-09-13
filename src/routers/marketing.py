"""Capa HTTP (delgada) de las encuestas de marketing. La lógica vive en src.services.marketing.

Dos públicos sobre el mismo recurso:

- `POST /marketing/surveys/{survey}/responses` es **público**: lo llama el formulario al que llega
  el médico desde el correo masivo, sin sesión. Con rate limit por IP, como toda escritura pública.
- `GET .../responses` y `GET .../responses/export` exigen `marketing.read`, sembrado **solo para
  super_admin** (ver `20260912_191154_seed_marketing_read_permission.sql`). Mismo contrato que
  `/reports/*` —vista previa `ReportPreview` + `.xlsx` auditado—, así el panel los pinta con la
  misma tabla genérica.
- `GET /marketing/surveys` (el número de cada pestaña) y `GET .../stats` (los gráficos), con el
  mismo permiso. Son agregados: no devuelven ningún correo.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.ratelimit import limiter
from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.routers.reports import (
    XLSX_MEDIA_TYPE,
    export_actor,
    export_filename,
    report_preview,
    xlsx_download,
)
from src.schemas.marketing import (
    MarketingPerformanceResponse,
    SurveyResponseCreate,
    SurveyResponseReceipt,
    SurveySlug,
    SurveyStatsResponse,
    SurveyTotalResponse,
)
from src.schemas.report import ReportPreview
from src.services import marketing as marketing_service
from src.services import marketing_performance

router = APIRouter(prefix="/marketing", tags=["marketing"])
tag_metadata = [
    {
        "name": "marketing",
        "description": (
            "Encuestas de marketing a médicos (psicólogos, especialistas y médicos generales): "
            "envío público de respuestas, y listado, exportación a Excel, totales y agregados "
            "para los gráficos del panel. Leer exige `marketing.read` (solo `super_admin`); cada "
            "exportación queda en `audit_log`."
        ),
    }
]

_FORBIDDEN = {403: {"description": "Requiere el permiso `marketing.read` (solo super_admin)."}}
_UNKNOWN_SURVEY = {422: {"description": "La encuesta no existe."}}


def response_filters(
    search: str | None = Query(None, description="Correo (ILIKE)."),
    answered_from: date | None = Query(
        None,
        description="Última respuesta desde esta fecha (inclusive, hora de Venezuela).",
    ),
    answered_to: date | None = Query(
        None,
        description="Última respuesta hasta esta fecha (inclusive, hora de Venezuela).",
    ),
) -> marketing_service.ResponseFilters:
    """Filtros del listado, declarados una sola vez para la vista previa y la exportación: si se
    escribieran aparte, un filtro nuevo llegaría a uno y no al otro."""
    return marketing_service.ResponseFilters(
        search=search, answered_from=answered_from, answered_to=answered_to
    )


def stats_filters(
    answered_from: date | None = Query(
        None,
        description="Última respuesta desde esta fecha (inclusive, hora de Venezuela).",
    ),
    answered_to: date | None = Query(
        None,
        description="Última respuesta hasta esta fecha (inclusive, hora de Venezuela).",
    ),
) -> marketing_service.ResponseFilters:
    """Los gráficos se acotan por fecha pero no por correo: un agregado de una sola persona no es
    un gráfico."""
    return marketing_service.ResponseFilters(answered_from=answered_from, answered_to=answered_to)


@router.post(
    "/surveys/{survey}/responses",
    response_model=SurveyResponseReceipt,
    status_code=status.HTTP_201_CREATED,
    summary="Responder una encuesta de marketing (público)",
    responses={
        400: {"description": "El campo trampa anti-bot (`website`) llegó con valor."},
        422: {
            "description": (
                "La encuesta no existe, una opción no es válida para esa encuesta, o falta una "
                "respuesta obligatoria (p. ej. la disponibilidad)."
            )
        },
        429: {"description": "Demasiados envíos desde esta IP (rate limit)."},
    },
)
@limiter.limit(settings.SURVEY_RESPONSE_RATE_LIMIT)
async def submit_survey_response(
    request: Request,
    survey: SurveySlug,
    payload: SurveyResponseCreate,
    db: AsyncSession = Depends(get_db),
) -> SurveyResponseReceipt:
    """Guarda la respuesta de un médico a la encuesta `survey`.

    Una respuesta por encuesta y correo: si ese correo ya la respondió, se **reemplaza** (la
    encuesta promete que se puede ajustar). Devuelve `201` en los dos casos; `created_at` distinto
    de `updated_at` indica que se actualizó una anterior.

    El correo llega en el enlace del correo masivo y no se verifica, así que la respuesta no
    devuelve lo guardado: sería enseñar lo que contestó otra persona con solo escribir su correo.

    Anti-bot: rate limit por IP + campo honeypot (`website`, debe ir vacío).

    `request` es obligatorio para slowapi (lee la IP del cliente), aunque no se use aquí."""
    saved = await marketing_service.submit_response(db, survey, payload)
    return SurveyResponseReceipt(
        survey=saved.survey, created_at=saved.created_at, updated_at=saved.updated_at
    )


@router.get(
    "/surveys",
    response_model=list[SurveyTotalResponse],
    summary="Total de respuestas de cada encuesta (super_admin)",
    responses={**_FORBIDDEN},
)
async def list_survey_totals(
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("marketing.read")),
) -> list[marketing_service.SurveyTotal]:
    """Cuántas respuestas tiene cada encuesta, sin filtros, en el orden de las pestañas del panel
    (psicólogos, especialistas, médicos generales). Una encuesta sin respuestas sale con `0`."""
    return await marketing_service.survey_totals(db)


@router.get(
    "/performance",
    response_model=MarketingPerformanceResponse,
    summary="Rendimiento de la campaña: de los envíos de Kit a las respuestas (super_admin)",
    responses={**_FORBIDDEN},
)
async def get_marketing_performance(
    refresh: bool = Query(
        False,
        description=(
            "Pedir de nuevo las métricas a Kit en vez de reutilizar las guardadas (como mucho "
            "una vez cada 30 segundos)."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("marketing.read")),
) -> marketing_performance.Performance:
    """El embudo de cada encuesta, de punta a punta: enviados, aperturas, clics y bajas (Kit), y
    respuestas (plataforma). Incluye también la línea de tiempo de las respuestas desde el primer
    envío.

    - Cada envío de Kit se asocia **solo** a su encuesta por el enlace `/encuesta/<slug>` que
      lleva (o, sin clics todavía, por el nombre de la plantilla). Varios envíos de una encuesta
      se suman.
    - `responses_after_send` cuenta las respuestas llegadas desde el primer envío: es el
      numerador honesto de la tasa de respuesta (las anteriores son pruebas u otros canales).
    - Kit se consulta de solo lectura y sus métricas se reutilizan unos minutos. Si falta la clave
      o Kit falla, responde igual `200` con `kit_status` explicándolo y esas métricas en `null`.

    Son agregados: no devuelve correos."""
    return await marketing_performance.campaign_performance(db, refresh=refresh)


@router.get(
    "/surveys/{survey}/stats",
    response_model=SurveyStatsResponse,
    summary="Agregados de una encuesta para los gráficos (super_admin)",
    responses={
        **_FORBIDDEN,
        422: {"description": "La encuesta no existe, o `role` no es una opción de esa encuesta."},
    },
)
async def get_survey_stats(
    survey: SurveySlug,
    role: str | None = Query(
        None,
        min_length=1,
        max_length=40,
        description=(
            "Código de una forma de participar de ESTA encuesta (p. ej. `atender_pacientes`): "
            "acota los gráficos a quienes la marcaron."
        ),
    ),
    filters: marketing_service.ResponseFilters = Depends(stats_filters),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("marketing.read")),
) -> marketing_service.SurveyStats:
    """Lo que hace falta para decidir con las respuestas, sin bajar al detalle de cada una:

    - **Cobertura por día y momento** (`availability`): cuántos marcaron cada par. Es cobertura
      posible, porque día y momento se preguntan por separado.
    - **Cómo quieren participar**, **días**, **momentos**, **horas a la semana** y, en psicólogos y
      especialistas, **dónde están**: cuántas respuestas marcaron cada opción, todas las opciones
      en el orden del formulario (también las que tienen 0).
    - **`min_weekly_hours`**: las horas semanales que, como mínimo, ofrecen entre todos.

    Con `role`, todo se calcula sobre quienes marcaron esa forma de participar (p. ej. la cobertura
    de quienes van a atender pacientes). No devuelve correos."""
    return await marketing_service.survey_stats(db, survey, filters, role=role)


@router.get(
    "/surveys/{survey}/responses",
    response_model=ReportPreview,
    summary="Respuestas de una encuesta (vista previa paginada, super_admin)",
    responses={**_FORBIDDEN, **_UNKNOWN_SURVEY},
)
async def list_survey_responses(
    survey: SurveySlug,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    filters: marketing_service.ResponseFilters = Depends(response_filters),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("marketing.read")),
) -> ReportPreview:
    """Una página de las respuestas de la encuesta, la más reciente primero, con las columnas que
    tendrá el Excel y el `total` exacto de respuestas que cumplen el filtro.

    Cada encuesta trae SUS columnas: médicos generales no tiene zona horaria pero sí "rol más
    activo"; psicólogos y especialistas al revés. Las opciones marcadas llegan ya como el texto que
    vio quien respondió."""
    report = await marketing_service.responses_report(db, survey, filters, skip=skip, limit=limit)
    return report_preview(report)


@router.get(
    "/surveys/{survey}/responses/export",
    summary="Exportar las respuestas de una encuesta a Excel (super_admin)",
    response_class=Response,
    responses={
        200: {
            "content": {XLSX_MEDIA_TYPE: {}},
            "description": "Libro .xlsx: hoja con las respuestas + portada con los filtros.",
        },
        **_FORBIDDEN,
        **_UNKNOWN_SURVEY,
    },
)
async def export_survey_responses(
    survey: SurveySlug,
    filters: marketing_service.ResponseFilters = Depends(response_filters),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("marketing.read")),
) -> Response:
    """El `.xlsx` con **todas** las respuestas que cumplen el filtro (no la página de la vista
    previa). Queda en `audit_log` como `report.exported` con `report = marketing-<encuesta>`."""
    return xlsx_download(
        await marketing_service.export_responses(db, survey, filters, **export_actor(principal)),
        export_filename(f"encuesta-{survey}"),
    )
