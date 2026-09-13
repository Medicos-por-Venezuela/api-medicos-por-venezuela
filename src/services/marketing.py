"""Encuestas de marketing: las respuestas del formulario público y su listado exportable.

Tres encuestas de re-targeting —psicólogos, especialistas y médicos generales— que se mandan por
correo masivo con un enlace a `/encuesta/<slug>?email=<correo>`. Este módulo guarda lo que contesta
cada médico y se lo sirve al panel (módulo Marketing) como listado y como Excel.

Cuatro decisiones que sostienen el módulo:

1. **Una respuesta por persona y encuesta.** Responder otra vez actualiza la fila, porque la
   encuesta promete que "si más adelante tu situación cambia, lo puedes ajustar". Es un upsert
   atómico (`ON CONFLICT (survey, email)`), no un read-then-write: dos envíos simultáneos del mismo
   correo no pueden crear dos filas ni fallar con un 409.
2. **Códigos, no textos.** Se guardan los códigos de las opciones y las etiquetas viven aquí. Cada
   encuesta tiene SUS etiquetas —el mismo `atender_pacientes` se lee distinto para un psicólogo
   que para un especialista—, así que el listado de cada pestaña dice lo que vio quien respondió.
3. **Lo que la encuesta no preguntó, no se guarda.** El texto de "Otra" sin haberla marcado, o la
   zona horaria en la encuesta de médicos generales, se descartan: el formulario no los enseña, y
   guardarlos daría respuestas que nadie contestó. La disponibilidad, en cambio, la preguntan y la
   exigen las tres encuestas.
4. **El listado reutiliza el esqueleto de los reportes** (`run_report`, `build_workbook`,
   `log_export`): la misma tabla genérica en el panel, el mismo tope de filas y la misma entrada en
   `audit_log` por cada exportación.

⚠️ El correo NO está verificado: quien conozca el enlace puede responder con otro correo y, como
la fila se actualiza, pisar la respuesta de otra persona. Es un techo aceptado a propósito (importa
el listado, no ligarlo a la ficha del médico). Por eso el endpoint público nunca devuelve lo
guardado y va con rate limit por IP.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import BadRequestError, UnprocessableError
from src.core.tz import day_bounds, to_local
from src.models.marketing_survey_response import MarketingSurveyResponse
from src.schemas.marketing import SurveyResponseCreate
from src.services.reports import (
    MAX_EXPORT_ROWS,
    Column,
    Report,
    build_workbook,
    describe_filters,
    guard_export_size,
    log_export,
    run_report,
)

# Código de "Otra forma que quiero proponerles" y de "Otra" en la zona horaria: las dos abren un
# campo de texto libre.
OTHER = "otra"

# --- Opciones comunes ---------------------------------------------------------
# El orden de cada diccionario es el del formulario: es el orden en que se guardan y se listan
# las opciones marcadas, llegaran como llegaran.

MOMENTS = {
    "manana": "Mañana",
    "tarde": "Tarde",
    "noche": "Noche",
    "variable": "Es variable",
}

DAYS = {
    "lunes": "Lunes",
    "martes": "Martes",
    "miercoles": "Miércoles",
    "jueves": "Jueves",
    "viernes": "Viernes",
    "sabado": "Sábado",
    "domingo": "Domingo",
    "variable": "Es variable",
}

WEEKLY_HOURS = {
    "menos_de_1": "Menos de 1 hora a la semana",
    "entre_1_y_3": "Entre 1 y 3 horas a la semana",
    "entre_3_y_6": "Entre 3 y 6 horas a la semana",
    "mas_de_6": "Más de 6 horas a la semana",
}

TIMEZONES = {
    "venezuela": "Venezuela (GMT-4)",
    "colombia_peru_ecuador": "Colombia, Perú, Ecuador (GMT-5)",
    "bolivia_chile_paraguay": "Bolivia, Chile, Paraguay (GMT-4)",
    "argentina_uruguay_brasil": "Argentina, Uruguay, Brasil (GMT-3)",
    "mexico_centro": "México · Centro (GMT-6)",
    "panama_costa_rica": "Panamá, Costa Rica (GMT-6)",
    "dominicana_puerto_rico": "República Dominicana, Puerto Rico (GMT-4)",
    "eeuu_canada_este": "Estados Unidos / Canadá · Este (GMT-5)",
    "eeuu_canada_centro": "Estados Unidos / Canadá · Centro (GMT-6)",
    "eeuu_canada_montana": "Estados Unidos / Canadá · Montaña (GMT-7)",
    "eeuu_canada_pacifico": "Estados Unidos / Canadá · Pacífico (GMT-8)",
    "reino_unido_portugal": "Reino Unido, Portugal (GMT+0)",
    "espana_italia_francia_alemania": "España, Italia, Francia, Alemania (GMT+1)",
    "europa_este": "Europa del Este (GMT+2)",
    "medio_oriente": "Medio Oriente (GMT+3)",
    "asia": "Asia (GMT+7 a GMT+9)",
    "australia_nueva_zelanda": "Australia y Nueva Zelanda (GMT+10 a GMT+12)",
    OTHER: "Otra",
}


# --- Las tres encuestas -------------------------------------------------------


@dataclass(frozen=True)
class Survey:
    """Lo que distingue a una encuesta de las otras dos.

    `active_detail_role`: rol que trae su propio "cuéntanos qué tienes en mente" (además del de
    "Otra"). `asks_timezone`: si pregunta dónde está.
    """

    slug: str
    title: str  # portada del Excel
    sheet_name: str  # hoja de datos (Excel acepta 31 caracteres como máximo)
    roles: dict[str, str]
    active_detail_role: str | None
    asks_timezone: bool


SURVEYS: dict[str, Survey] = {
    "psicologos": Survey(
        slug="psicologos",
        title="Encuesta de psicólogos",
        sheet_name="Psicólogos",
        roles={
            "atender_pacientes": "Atender pacientes a través de la plataforma",
            "responder_interconsultas": (
                "Responder interconsultas (estudios de caso) cuando un colega lo necesite"
            ),
            "rol_activo": (
                "Asumir un rol más activo (coordinar, liderar) dentro del equipo de psicología"
            ),
            OTHER: "Otra forma que quiero proponerles",
        },
        active_detail_role=None,
        asks_timezone=True,
    ),
    "especialistas": Survey(
        slug="especialistas",
        title="Encuesta de especialistas",
        sheet_name="Especialistas",
        roles={
            "responder_interconsultas": "Responder interconsultas cuando pueda",
            "atender_pacientes": "Atender pacientes directamente en mi especialidad",
            "atender_y_responder": "Atender pacientes y responder interconsultas",
            "coordinar_especialidad": "Coordinar mi especialidad dentro de la red",
            OTHER: "Otra forma que quiero proponerles",
        },
        active_detail_role=None,
        asks_timezone=True,
    ),
    # Sin zona horaria: la encuesta va a médicos en Venezuela ("Horas de Venezuela"). La
    # disponibilidad se pregunta a todos, como en las otras dos: al principio solo se pedía a quien
    # iba a atender, liderar o proponer otra cosa, y se cambió para enseñar el formulario completo.
    "medicos-generales": Survey(
        slug="medicos-generales",
        title="Encuesta de médicos generales",
        sheet_name="Médicos generales",
        roles={
            "pedir_interconsultas": "Pedir interconsultas cuando tenga un caso que lo necesite",
            "atender_pacientes": "Seguir atendiendo pacientes a través de la plataforma",
            "rol_activo": "Asumir un rol más activo (coordinar, liderar)",
            OTHER: "Otra forma que quiero proponerles",
        },
        active_detail_role="rol_activo",
        asks_timezone=False,
    ),
}


# --- Envío (público) ----------------------------------------------------------


def _text(value: str | None) -> str | None:
    """Texto libre recortado. Vacío o solo espacios cuenta como no respondido."""
    if value is None:
        return None
    return value.strip() or None


def _codes(values: list[str], allowed: dict[str, str], question: str) -> list[str]:
    """Valida los códigos marcados y los devuelve sin repetir y en el orden del formulario.

    El orden canónico —y no el de llegada— hace que dos respuestas con las mismas opciones se
    lean igual en el listado, y que el Excel se pueda filtrar por texto sin sorpresas."""
    if unknown := sorted(set(values) - allowed.keys()):
        raise UnprocessableError(f"Opción no válida en «{question}»: {', '.join(unknown)}.")
    chosen = set(values)
    return [code for code in allowed if code in chosen]


def _code(value: str | None, allowed: dict[str, str], question: str) -> str | None:
    """Como `_codes`, para una pregunta de respuesta única."""
    if value is None:
        return None
    if value not in allowed:
        raise UnprocessableError(f"Opción no válida en «{question}»: {value}.")
    return value


def normalize_response(survey: Survey, payload: SurveyResponseCreate) -> dict:
    """Las columnas a guardar: códigos validados contra ESTA encuesta, obligatorias exigidas y lo
    que la encuesta no preguntó, descartado.

    Las obligatorias se exigen aquí y no solo en el formulario: el endpoint es público, y una
    respuesta sin disponibilidad no le sirve a nadie para organizar la red.
    """
    # Honeypot: si el campo trampa llegó con valor, es un bot. Rechazo genérico, igual que en el
    # registro de médicos: decirle qué lo delató solo le enseña a esquivarlo.
    if payload.website:
        raise BadRequestError("Solicitud inválida.")
    roles = _codes(payload.roles, survey.roles, "Cómo quieres participar")

    moments = _codes(payload.moments, MOMENTS, "Momento del día")
    days = _codes(payload.days, DAYS, "Días de la semana")
    weekly_hours = _code(payload.weekly_hours, WEEKLY_HOURS, "Horas a la semana")
    if not moments:
        raise UnprocessableError("Indica en qué momento del día te resulta más fácil conectarte.")
    if not days:
        raise UnprocessableError("Indica qué días de la semana te quedan mejor.")
    if weekly_hours is None:
        raise UnprocessableError("Indica cuántas horas a la semana podrías dedicar.")

    timezone = timezone_other = None
    if survey.asks_timezone:
        timezone = _code(payload.timezone, TIMEZONES, "Dónde estás")
        if timezone is None:
            raise UnprocessableError("Elige tu país o zona horaria.")
        if timezone == OTHER:
            timezone_other = _text(payload.timezone_other)

    return {
        "survey": survey.slug,
        # Minúsculas y sin espacios: es la mitad de la clave única, y "Ana@x.com" y "ana@x.com "
        # no pueden contar como dos personas.
        "email": str(payload.email).strip().lower(),
        "roles": roles,
        "role_active_detail": (
            _text(payload.role_active_detail) if survey.active_detail_role in roles else None
        ),
        "role_other_detail": _text(payload.role_other_detail) if OTHER in roles else None,
        "moments": moments,
        "days": days,
        "weekly_hours": weekly_hours,
        "availability_notes": _text(payload.availability_notes),
        "timezone": timezone,
        "timezone_other": timezone_other,
        "notes": _text(payload.notes),
    }


async def submit_response(
    session: AsyncSession, survey_slug: str, payload: SurveyResponseCreate
) -> MarketingSurveyResponse:
    """Guarda la respuesta; si ese correo ya respondió esta encuesta, la reemplaza.

    Un único `INSERT ... ON CONFLICT DO UPDATE`: la base resuelve la carrera entre dos envíos del
    mismo correo, no la app. Se reemplaza TODO lo respondido menos `created_at`, que conserva la
    fecha de la primera respuesta.
    """
    values = normalize_response(SURVEYS[survey_slug], payload)
    stmt = insert(MarketingSurveyResponse).values(**values)
    stmt = (
        stmt.on_conflict_do_update(
            index_elements=[MarketingSurveyResponse.survey, MarketingSurveyResponse.email],
            set_={
                **{key: stmt.excluded[key] for key in values if key not in ("survey", "email")},
                "updated_at": func.now(),
            },
        )
        .returning(MarketingSurveyResponse)
        # Si la fila ya estaba cargada en la sesión, sin esto el ORM devuelve el objeto viejo
        # (con el `updated_at` anterior) en vez del que acaba de devolver la base.
        .execution_options(populate_existing=True)
    )
    saved = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return saved


# --- Listado (panel) ----------------------------------------------------------


@dataclass(frozen=True)
class ResponseFilters:
    """Filtros del listado de respuestas de una encuesta."""

    search: str | None = None
    # Sobre la ÚLTIMA respuesta (`updated_at`): quien respondió antes del envío y ajustó después
    # cuenta como respuesta a ese envío.
    answered_from: date | None = None
    answered_to: date | None = None


def describe_response_filters(f: ResponseFilters) -> list[tuple[str, str]]:
    """Los filtros aplicados, legibles, para la portada del Excel y los chips del panel."""
    return describe_filters(
        [
            ("Búsqueda (correo)", f.search),
            ("Respondieron desde", f.answered_from),
            ("Respondieron hasta", f.answered_to),
        ]
    )


def survey_columns(survey: Survey) -> tuple[Column, ...]:
    """Columnas del listado de UNA encuesta: las preguntas que esa encuesta no hace no salen, para
    que el Excel de médicos generales no traiga dos columnas de zona horaria siempre vacías."""
    columns = [
        Column("email", "Correo", 32),
        Column("updated_at", "Última respuesta", 18, "datetime"),
        Column("roles", "Cómo quiere participar", 60),
    ]
    if survey.active_detail_role:
        columns.append(Column("role_active_detail", "Rol más activo: qué tiene en mente", 40))
    columns += [
        Column("role_other_detail", "Otra forma: su propuesta", 40),
        Column("moments", "Momento del día", 26),
        Column("days", "Días de la semana", 34),
        Column("weekly_hours", "Horas a la semana", 30),
        Column("availability_notes", "Disponibilidad en sus palabras", 44),
    ]
    if survey.asks_timezone:
        columns += [
            Column("timezone", "Dónde está", 40),
            Column("timezone_other", "Otra ubicación", 26),
        ]
    columns += [
        Column("notes", "Algo más", 44),
        Column("created_at", "Primera respuesta", 18, "datetime"),
    ]
    return tuple(columns)


def _labels(codes: list[str], labels: dict[str, str]) -> str:
    """Códigos -> etiquetas separadas por "; " (no por comas: varias etiquetas llevan comas).

    Un código que ya no esté en el diccionario sale tal cual en vez de desaparecer: si mañana se
    retira una opción, las respuestas viejas siguen diciendo qué marcaron."""
    return "; ".join(labels.get(code, code) for code in codes)


def response_row(survey: Survey, r: MarketingSurveyResponse, columns: tuple[Column, ...]) -> dict:
    """Una fila del listado, ya presentada (etiquetas en español, fechas en hora de Caracas)."""
    values = {
        "email": r.email,
        "updated_at": to_local(r.updated_at),
        "roles": _labels(r.roles, survey.roles),
        "role_active_detail": r.role_active_detail,
        "role_other_detail": r.role_other_detail,
        "moments": _labels(r.moments, MOMENTS),
        "days": _labels(r.days, DAYS),
        "weekly_hours": WEEKLY_HOURS.get(r.weekly_hours or "", r.weekly_hours),
        "availability_notes": r.availability_notes,
        "timezone": TIMEZONES.get(r.timezone or "", r.timezone),
        "timezone_other": r.timezone_other,
        "notes": r.notes,
        "created_at": to_local(r.created_at),
    }
    return {column.key: values[column.key] for column in columns}


def responses_query(survey_slug: str, filters: ResponseFilters) -> Select:
    """Consulta del listado. ÚNICA definición de los filtros: la comparten la vista previa y la
    exportación, así que el Excel trae exactamente lo que enseña la tabla."""
    stmt = select(MarketingSurveyResponse).where(MarketingSurveyResponse.survey == survey_slug)
    if filters.search and (term := filters.search.strip()):
        stmt = stmt.where(MarketingSurveyResponse.email.ilike(f"%{term}%"))
    start, end = day_bounds(filters.answered_from, filters.answered_to)
    if start is not None:
        stmt = stmt.where(MarketingSurveyResponse.updated_at >= start)
    if end is not None:
        stmt = stmt.where(MarketingSurveyResponse.updated_at < end)
    # La respuesta más reciente primero, con el id de desempate: sin él, dos respuestas del mismo
    # instante pueden repetirse u omitirse entre páginas.
    return stmt.order_by(MarketingSurveyResponse.updated_at.desc(), MarketingSurveyResponse.id)


async def responses_report(
    session: AsyncSession,
    survey_slug: str,
    filters: ResponseFilters,
    *,
    skip: int | None = None,
    limit: int | None = None,
) -> Report:
    """Respuestas de una encuesta. Sin `limit` devuelve todas (exportación)."""
    survey = SURVEYS[survey_slug]
    columns = survey_columns(survey)
    return await run_report(
        session,
        responses_query(survey.slug, filters),
        columns,
        lambda r: response_row(survey, r[0], columns),
        describe_response_filters(filters),
        skip=skip,
        limit=limit,
    )


async def export_responses(
    session: AsyncSession,
    survey_slug: str,
    filters: ResponseFilters,
    *,
    actor_user_id: uuid.UUID,
    actor_label: str,
) -> bytes:
    """El `.xlsx` con todas las respuestas que cumplen el filtro + su entrada de audit.

    Es la lista de correos de quienes respondieron, en un archivo que sale de la plataforma: queda
    en `audit_log` como `report.exported` (con `report = marketing-<encuesta>`), igual que los
    reportes."""
    survey = SURVEYS[survey_slug]
    report = await responses_report(session, survey_slug, filters, limit=MAX_EXPORT_ROWS + 1)
    guard_export_size(report.total)
    await log_export(
        session,
        report_name=f"marketing-{survey.slug}",
        actor_user_id=actor_user_id,
        report=report,
    )
    return build_workbook(
        report, title=survey.title, sheet_name=survey.sheet_name, generated_by=actor_label
    )
