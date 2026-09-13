"""Capa HTTP (delgada) de las encuestas de marketing. La lógica vive en src.services.marketing.

Dos públicos sobre el mismo recurso:

- `POST /marketing/surveys/{survey}/responses` es **público**: lo llama el formulario al que llega
  el médico desde el correo masivo, sin sesión. Con rate limit por IP, como toda escritura pública.
- `GET .../responses` y `GET .../responses/export` exigen `marketing.read`, sembrado **solo para
  super_admin** (ver `20260912_191154_seed_marketing_read_permission.sql`). Mismo contrato que
  `/reports/*` —vista previa `ReportPreview` + `.xlsx` auditado—, así el panel los pinta con la
  misma tabla genérica.
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
from src.schemas.marketing import SurveyResponseCreate, SurveyResponseReceipt, SurveySlug
from src.schemas.report import ReportPreview
from src.services import marketing as marketing_service

router = APIRouter(prefix="/marketing", tags=["marketing"])
tag_metadata = [
    {
        "name": "marketing",
        "description": (
            "Encuestas de marketing a médicos (psicólogos, especialistas y médicos generales): "
            "envío público de respuestas, y listado y exportación a Excel para el panel. Leer "
            "exige `marketing.read` (solo `super_admin`); cada exportación queda en `audit_log`."
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
                "respuesta obligatoria (p. ej. la disponibilidad de quien quiere atender)."
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
