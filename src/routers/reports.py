"""Capa HTTP (delgada) de los reportes. La lógica vive en src.services.reports.

Cada reporte expone DOS endpoints sobre la misma consulta y los mismos filtros:

- `GET /reports/{recurso}` — vista previa en JSON, paginada, para pintar la tabla y ver el
  `total` que se va a exportar antes de descargar nada.
- `GET /reports/{recurso}/export` — el `.xlsx` con **todas** las filas que cumplen el filtro.

Autorización: `reports.export` en los dos, sembrado **solo para `super_admin`** (ver la
migración `20260903_093414_seed_reports_export_permission.sql`). La vista previa comparte el
permiso del export a propósito: enseña exactamente la misma PII, fila a fila, así que gatearla
más flojo sería regalar por JSON lo que se protege en el archivo.
"""

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import UnprocessableError
from src.core.security import Principal, require_permission
from src.core.tz import to_local
from src.db.session import get_db
from src.models.consultation import CONSULTATION_STATUSES
from src.schemas.doctor import DoctorBlockedReason
from src.schemas.report import ReportPreview
from src.services import reports as reports_service

router = APIRouter(prefix="/reports", tags=["reports"])
tag_metadata = [
    {
        "name": "reports",
        "description": (
            "Reportes de listado de médicos, pacientes y consultas: vista previa filtrable y "
            "exportación "
            "a Excel de la población completa. Solo `super_admin` (permiso `reports.export`); "
            "cada exportación queda registrada en `audit_log`."
        ),
    }
]

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_TOO_MANY = {
    422: {
        "description": (
            f"El filtro devuelve más de {reports_service.MAX_EXPORT_ROWS} filas: acótalo "
            "(por fecha, estado o búsqueda) antes de exportar."
        )
    }
}
_FORBIDDEN = {403: {"description": "Requiere el permiso `reports.export` (solo super_admin)."}}


def _preview(report: reports_service.Report) -> ReportPreview:
    """`Report` (dominio) -> `ReportPreview` (contrato HTTP)."""
    return ReportPreview(
        columns=[c for c in report.columns],
        rows=report.rows,
        total=report.total,
        filters=[[label, value] for label, value in report.filters],
    )


def _xlsx(content: bytes, filename: str) -> Response:
    """Devuelve los bytes del libro como descarga.

    `Response` y no `StreamingResponse`: el archivo ya está completo en memoria (xlsxwriter no
    entrega un generador de bytes), así que envolverlo en un stream solo escondería el
    `Content-Length` que el navegador necesita para mostrar el progreso de la descarga.
    """
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _actor(principal: Principal) -> dict:
    """Quién exporta, en datos planos: el servicio audita y firma la portada sin conocer
    `Principal` (la capa de negocio no sabe de autenticación). El email es la etiqueta legible;
    si la cuenta no tiene, cae al id — la portada nunca queda sin firmar."""
    return {"actor_user_id": principal.id, "actor_label": principal.email or str(principal.id)}


def _filename(prefix: str) -> str:
    """`medicos-2026-09-03.xlsx`, con la fecha del día **en Venezuela** y no en UTC: a las 21:00
    de Caracas ya es el día siguiente en UTC, y el archivo llevaría la fecha de mañana."""
    return f"{prefix}-{to_local(datetime.now(UTC)):%Y-%m-%d}.xlsx"


# --- Filtros como dependencias -----------------------------------------------
# Declarar los Query() una sola vez por reporte y no dos (vista previa + export) no es solo
# ahorro de líneas: si las dos listas se escriben aparte, cualquier filtro nuevo llega a una y
# no a la otra, y el botón "Exportar" deja de exportar lo que la tabla enseña.


def doctor_filters(
    status: int | None = Query(
        None, ge=0, le=2, description="0=de baja · 1=activo · 2=expulsado."
    ),
    verified: bool | None = Query(
        None, description="Credencial aprobada (SACS/FPV o aprobación manual)."
    ),
    can_practice: bool | None = Query(
        None,
        description="true=habilitados para atender · false=bloqueados. NO es lo mismo que "
        "`verified`: exige además ficha activa, cédula y licencia.",
    ),
    blocked_reason: DoctorBlockedReason | None = Query(
        None, description="Motivo de bloqueo exacto (`sin_ficha`, `sin_cedula`, …)."
    ),
    search: str | None = Query(None, description="Nombre, cédula o email (ILIKE)."),
    specialty_id: uuid.UUID | None = Query(None),
    professional_type_id: uuid.UUID | None = Query(None),
    created_from: date | None = Query(
        None, description="Registrados desde esta fecha (inclusive, hora de Venezuela)."
    ),
    created_to: date | None = Query(
        None, description="Registrados hasta esta fecha (inclusive, hora de Venezuela)."
    ),
) -> reports_service.DoctorFilters:
    """Filtros del reporte de médicos, compartidos por la vista previa y la exportación."""
    return reports_service.DoctorFilters(
        status=status,
        verified=verified,
        can_practice=can_practice,
        blocked_reason=blocked_reason,
        search=search,
        specialty_id=specialty_id,
        professional_type_id=professional_type_id,
        created_from=created_from,
        created_to=created_to,
    )


def patient_filters(
    search: str | None = Query(None, description="Nombre, cédula, email o teléfono (ILIKE)."),
    origin: str | None = Query(
        None,
        pattern="^(publica|consultorio)$",
        description="`publica` = alta del propio paciente · `consultorio` = registrado por su "
        "médico para una interconsulta.",
    ),
    affected_zone: str | None = Query(None, description="Zona afectada exacta (del catálogo)."),
    age_range: str | None = Query(None, description="Rango de edad exacto."),
    need_tag: str | None = Query(
        None, description="Una de las necesidades declaradas (`needs_tags` contiene el valor)."
    ),
    has_account: bool | None = Query(
        None, description="true=tiene cuenta para seguir su caso (`/mi-caso`)."
    ),
    has_consultations: bool | None = Query(
        None, description="false = registrados que nunca llegaron a tener un caso."
    ),
    consent: bool | None = Query(None, description="Consentimiento otorgado."),
    include_archived: bool = Query(
        False, description="Incluir pacientes archivados (baja lógica). Por defecto quedan fuera."
    ),
    created_from: date | None = Query(
        None, description="Registrados desde esta fecha (inclusive, hora de Venezuela)."
    ),
    created_to: date | None = Query(
        None, description="Registrados hasta esta fecha (inclusive, hora de Venezuela)."
    ),
) -> reports_service.PatientFilters:
    """Filtros del reporte de pacientes, compartidos por la vista previa y la exportación."""
    return reports_service.PatientFilters(
        search=search,
        origin=origin,
        affected_zone=affected_zone,
        age_range=age_range,
        need_tag=need_tag,
        has_account=has_account,
        has_consultations=has_consultations,
        consent=consent,
        include_archived=include_archived,
        created_from=created_from,
        created_to=created_to,
    )


def consultation_filters(
    status: list[str] | None = Query(
        None,
        description="Estados a incluir (repetible). Omitir = todos. El panel manda por defecto "
        "el set del monitor: `in_progress`, `referred_to_specialist`, `urgent_in_person`, "
        "`patient_no_show`, `cancelled`, `contacted_whatsapp`.",
    ),
    assigned_doctor_id: uuid.UUID | None = Query(
        None, description="Id de CUENTA del médico asignado (no el de su ficha en `doctors`)."
    ),
    specialty_id: uuid.UUID | None = Query(None, description="Especialidad solicitada."),
    unassigned: bool | None = Query(
        None, description="true = solo casos sin médico asignado · false = solo asignados."
    ),
    search: str | None = Query(
        None, description="Paciente, código, motivo de consulta o médico (ILIKE)."
    ),
    created_from: date | None = Query(
        None, description="Creadas desde esta fecha (inclusive, hora de Venezuela)."
    ),
    created_to: date | None = Query(
        None, description="Creadas hasta esta fecha (inclusive, hora de Venezuela)."
    ),
) -> reports_service.ConsultationFilters:
    """Filtros del reporte de consultas, compartidos por la vista previa y la exportación.

    Los estados se validan contra `CONSULTATION_STATUSES` (el mismo CHECK que tiene la tabla):
    un estado inventado devolvería cero filas en silencio, y quien lo pidió leería ese vacío
    como "no hay casos" en vez de como "escribiste mal el filtro".
    """
    estados = tuple(status or ())
    if desconocidos := sorted(set(estados) - CONSULTATION_STATUSES):
        raise UnprocessableError(f"Estados no válidos: {', '.join(desconocidos)}.")
    return reports_service.ConsultationFilters(
        statuses=estados,
        assigned_doctor_id=assigned_doctor_id,
        specialty_id=specialty_id,
        unassigned=unassigned,
        search=search,
        created_from=created_from,
        created_to=created_to,
    )


# --- Médicos ------------------------------------------------------------------


@router.get(
    "/doctors",
    response_model=ReportPreview,
    summary="Vista previa del reporte de médicos (paginada, super_admin)",
    responses=_FORBIDDEN,
)
async def preview_doctors_report(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    filters: reports_service.DoctorFilters = Depends(doctor_filters),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("reports.export")),
) -> ReportPreview:
    """Una página del reporte de médicos, con las columnas que tendrá el Excel y el `total`
    exacto de filas que cumplen el filtro.

    Es para **ajustar el filtro antes de descargar**: el `total` dice cuántas filas traería el
    archivo, así que se ve si el recorte es el que se quería sin generar un xlsx de 3000 filas
    para descubrir que faltaba una fecha.

    Cada fila trae los nombres ya resueltos (especialidad, tipo profesional), el estado de
    credencial (`Habilitado para atender` + `Motivo de bloqueo`, mismo criterio que el gate de
    acceso de los médicos) y su actividad (consultas asignadas, cerradas y última)."""
    report = await reports_service.doctors_report(db, filters, skip=skip, limit=limit)
    return _preview(report)


@router.get(
    "/doctors/export",
    summary="Exportar el reporte de médicos a Excel (super_admin)",
    response_class=Response,
    responses={
        200: {
            "content": {XLSX_MEDIA_TYPE: {}},
            "description": "Libro .xlsx: hoja `Médicos` + portada con los filtros aplicados.",
        },
        **_FORBIDDEN,
        **_TOO_MANY,
    },
)
async def export_doctors_report(
    filters: reports_service.DoctorFilters = Depends(doctor_filters),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("reports.export")),
) -> Response:
    """El `.xlsx` con **todos** los médicos que cumplen el filtro (no la página de la vista
    previa): mismos filtros, mismas columnas, sin `limit`.

    El libro trae dos hojas: `Médicos` con los datos (cabecera fija, autofiltro) y `Reporte`
    con quién exportó, cuándo y qué filtros se aplicaron — sin eso, a la semana nadie sabe si
    el listado incluía a los médicos de baja.

    Queda registrado en `audit_log` como `report.exported`: es una extracción de PII médica de
    miles de personas a un archivo que sale de la plataforma, y quién la hizo debe ser
    reconstruible."""
    return _xlsx(
        await reports_service.export_doctors(db, filters, **_actor(principal)),
        _filename("medicos"),
    )


# --- Pacientes ----------------------------------------------------------------


@router.get(
    "/patients",
    response_model=ReportPreview,
    summary="Vista previa del reporte de pacientes (paginada, super_admin)",
    responses=_FORBIDDEN,
)
async def preview_patients_report(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    filters: reports_service.PatientFilters = Depends(patient_filters),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("reports.export")),
) -> ReportPreview:
    """Una página del reporte de pacientes, con las columnas que tendrá el Excel y el `total`
    exacto de filas que cumplen el filtro (ver la vista previa de médicos).

    Cada fila trae la ficha completa, el origen (cola pública o consultorio) con el médico que
    lo registró, su adulto responsable si es carga familiar, y la actividad de sus casos
    (cuántos, cuántos cerrados, y el estado del último — que es lo que dice si ese paciente
    sigue esperando)."""
    report = await reports_service.patients_report(db, filters, skip=skip, limit=limit)
    return _preview(report)


@router.get(
    "/patients/export",
    summary="Exportar el reporte de pacientes a Excel (super_admin)",
    response_class=Response,
    responses={
        200: {
            "content": {XLSX_MEDIA_TYPE: {}},
            "description": "Libro .xlsx: hoja `Pacientes` + portada con los filtros aplicados.",
        },
        **_FORBIDDEN,
        **_TOO_MANY,
    },
)
async def export_patients_report(
    filters: reports_service.PatientFilters = Depends(patient_filters),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("reports.export")),
) -> Response:
    """El `.xlsx` con **todos** los pacientes que cumplen el filtro: mismos filtros, mismas
    columnas, sin `limit`.

    Incluye alergias, descripción del caso y teléfono: es la extracción de PII médica más
    sensible de la API. Queda en `audit_log` como `report.exported`."""
    return _xlsx(
        await reports_service.export_patients(db, filters, **_actor(principal)),
        _filename("pacientes"),
    )


# --- Consultas ----------------------------------------------------------------


@router.get(
    "/consultations",
    response_model=ReportPreview,
    summary="Vista previa del reporte de consultas (paginada, super_admin)",
    responses={**_FORBIDDEN, 422: {"description": "Algún estado del filtro no existe."}},
)
async def preview_consultations_report(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    filters: reports_service.ConsultationFilters = Depends(consultation_filters),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("reports.export")),
) -> ReportPreview:
    """Una página del reporte de consultas, con las columnas que tendrá el Excel y el `total`
    exacto de filas que cumplen el filtro.

    Las cinco primeras columnas son, en orden, las del modal **Consultas en progreso** del
    dashboard (estado, médico asignado, paciente, tiempo en progreso y motivo); detrás vienen
    las que una hoja de cálculo necesita y una tabla en pantalla no: el código para cruzar con
    otros informes, las fechas para ordenar y el teléfono para actuar sin volver al panel.

    **Puede traer más filas que ese modal**: el monitor pide una página de 100 por estado
    (`GET /consultations` solo acepta un `status` a la vez), mientras que esto es una sola
    consulta sin tope por estado."""
    report = await reports_service.consultations_report(db, filters, skip=skip, limit=limit)
    return _preview(report)


@router.get(
    "/consultations/export",
    summary="Exportar el reporte de consultas a Excel (super_admin)",
    response_class=Response,
    responses={
        200: {
            "content": {XLSX_MEDIA_TYPE: {}},
            "description": "Libro .xlsx: hoja `Consultas` + portada con los filtros aplicados.",
        },
        **_FORBIDDEN,
        **_TOO_MANY,
        422: {"description": "Algún estado del filtro no existe, o el filtro excede el tope."},
    },
)
async def export_consultations_report(
    filters: reports_service.ConsultationFilters = Depends(consultation_filters),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("reports.export")),
) -> Response:
    """El `.xlsx` con **todas** las consultas que cumplen el filtro: mismos filtros, mismas
    columnas, sin `limit`.

    Incluye el motivo de consulta, que es contenido clínico escrito por el paciente. Queda
    registrado en `audit_log` como `report.exported`."""
    return _xlsx(
        await reports_service.export_consultations(db, filters, **_actor(principal)),
        _filename("consultas"),
    )
