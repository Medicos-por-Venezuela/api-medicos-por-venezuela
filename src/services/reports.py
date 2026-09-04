"""Reportes exportables de médicos y pacientes (listado filtrado -> Excel).

Qué resuelve: el panel admin muestra páginas de 50-100 filas; para un informe hace falta la
población COMPLETA que cumple unos filtros, con los nombres ya resueltos (especialidad, tipo
profesional, médico que registró al paciente) y las cifras que solo se pueden calcular en la
base (consultas atendidas, última consulta). Sacarlo desde el panel obligaba a paginar y pegar
a mano.

Tres decisiones que sostienen el módulo:

1. **Una sola definición de filtro por reporte.** `*_query()` la usan tanto la vista previa
   (JSON, paginada) como la exportación (Excel, completa). "Exportas lo que estás viendo" solo
   es verdad si hay UNA implementación del predicado; para médicos incluso se reutiliza la del
   panel admin (`doctors.apply_doctor_filters`).
2. **Sin N+1.** Los agregados por médico/paciente salen de subconsultas agrupadas que se unen
   una vez, no de una consulta por fila: un reporte de 3000 médicos con dos consultas por fila
   serían 6000 roundtrips.
3. **El Excel no decide nada.** `build_workbook` recibe filas ya construidas por `*_row()` y
   solo las pinta. Así la vista previa y el archivo no pueden divergir.

⚠️ La salida es PII médica completa (cédulas, teléfonos, alergias) de miles de personas en un
archivo que sale de la plataforma. El acceso se gatea en el router con `require_super_admin`
(no con un permiso, que un seed podría mapear a otro rol) y cada exportación deja su entrada
en `audit_log`.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from io import BytesIO

import xlsxwriter
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.core.errors import UnprocessableError
from src.core.tz import day_bounds, to_local
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.professional_type import ProfessionalType
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.services import audit
from src.services.doctors import admin_universe, apply_doctor_filters

# Estados de `consultations` que cuentan como caso cerrado (mismo criterio que el panel admin).
CLOSED_STATUSES = ("closed", "closed_by_admin")

# Etiquetas en español para el archivo: un informe con `patient_no_show` no es un informe.
STATUS_LABELS = {
    "waiting": "Esperando",
    "in_progress": "Abierta",
    "scheduled": "Agendada",
    "referred_to_specialist": "Derivada a especialista",
    "urgent_in_person": "Atención presencial urgente",
    "closed": "Cerrada",
    "cancelled": "Cancelada",
    "patient_no_show": "Paciente no se presentó",
    "closed_by_admin": "Cerrada por admin",
    "contacted_whatsapp": "Contactado vía WhatsApp",
}

DOCTOR_STATUS_LABELS = {0: "De baja", 1: "Activo", 2: "Expulsado"}

# El set amplio "en progreso" del monitor del dashboard: un médico ya tomó el caso y todavía no
# hay un cierre formal. Incluye los desenlaces derivados (derivada, presencial urgente) y los
# negativos (no-show, cancelada), porque para operación todos son "esto salió de la cola y hay
# que saber en qué quedó". Misma definición que `IN_PROGRESS_STATUSES` del frontend; vive aquí
# para que el Excel y el modal no puedan enseñar poblaciones distintas.
IN_PROGRESS_STATUSES = (
    "in_progress",
    "referred_to_specialist",
    "urgent_in_person",
    "patient_no_show",
    "cancelled",
    "contacted_whatsapp",
)

BLOCKED_REASON_LABELS = {
    "sin_ficha": "Sin ficha de médico",
    "de_baja": "Ficha de baja o expulsada",
    "sin_cedula": "Sin cédula",
    "sin_licencia": "Sin licencia",
    "no_verificado": "Credencial no verificada",
}

# Tope duro de filas por exportación. No es una paginación disfrazada: es lo que convierte un
# error de filtro ("exportar todo") en un 422 explicable, en vez de en un proceso que se come
# la memoria del contenedor y tumba la API para todos.
MAX_EXPORT_ROWS = 50_000


# --- Utilidades de presentación ----------------------------------------------


def _si_no(value: bool | None, *, unknown: str = "—") -> str:
    """Booleano legible. `None` NO es "No": distingue "no tiene cuenta" de "cuenta inactiva"."""
    if value is None:
        return unknown
    return "Sí" if value else "No"


# --- Definición de columnas ---------------------------------------------------


@dataclass(frozen=True)
class Column:
    """Una columna del reporte: clave en la fila, cabecera visible y ancho en el Excel.

    `kind="datetime"` marca las que llevan formato de fecha/hora en la hoja; el resto se
    escriben tal cual (texto o número).
    """

    key: str
    header: str
    width: int = 18
    kind: str = "text"


DOCTOR_COLUMNS: tuple[Column, ...] = (
    Column("full_name", "Nombre completo", 30),
    Column("cedula", "Cédula", 14),
    Column("license", "Licencia (MPPS/FPV)", 18),
    Column("email", "Email", 30),
    Column("phone", "Teléfono", 18),
    Column("professional_type", "Tipo profesional", 18),
    Column("specialty", "Especialidad", 24),
    Column("country", "País de residencia", 18),
    Column("status", "Estado de la ficha", 16),
    Column("verified", "Credencial verificada", 18),
    Column("can_practice", "Habilitado para atender", 20),
    Column("blocked_reason", "Motivo de bloqueo", 24),
    Column("has_account", "Tiene cuenta", 12),
    Column("account_role", "Rol de la cuenta", 16),
    Column("account_active", "Cuenta activa", 13),
    Column("last_seen_at", "Última conexión", 18, "datetime"),
    Column("consultations_total", "Consultas asignadas", 18),
    Column("consultations_closed", "Consultas cerradas", 18),
    Column("last_consultation_at", "Última consulta", 18, "datetime"),
    Column("created_at", "Fecha de registro", 18, "datetime"),
    Column("doctor_id", "ID ficha", 38),
    Column("user_id", "ID cuenta", 38),
)

PATIENT_COLUMNS: tuple[Column, ...] = (
    Column("full_name", "Nombre completo", 30),
    Column("cedula", "Cédula", 14),
    Column("age_range", "Rango de edad", 14),
    Column("phone_whatsapp", "Teléfono / WhatsApp", 18),
    Column("email", "Email", 30),
    Column("affected_zone", "Zona afectada", 22),
    Column("needs_tags", "Necesidades", 34),
    Column("description", "Descripción del caso", 50),
    Column("allergies", "Alergias", 26),
    Column("origin", "Origen", 16),
    Column("registered_by", "Registrado por (médico)", 26),
    Column("consent", "Consentimiento", 14),
    Column("consent_at", "Fecha de consentimiento", 20, "datetime"),
    Column("has_account", "Tiene cuenta", 12),
    Column("parent_name", "Adulto responsable", 26),
    Column("parentesco", "Parentesco", 14),
    Column("consultations_total", "Consultas", 11),
    Column("consultations_closed", "Consultas cerradas", 18),
    Column("last_consultation_at", "Última consulta", 18, "datetime"),
    Column("last_consultation_status", "Estado última consulta", 22),
    Column("last_consultation_code", "Código última consulta", 20),
    Column("archived", "Archivado", 11),
    Column("created_at", "Fecha de registro", 18, "datetime"),
    Column("patient_id", "ID paciente", 38),
)


# Las CINCO primeras columnas son, en orden, las del modal "Consultas en progreso" del
# dashboard. Es deliberado: el informe tiene que ser reconocible como "esa tabla" para quien lo
# pidió. Lo que viene después es lo que una tabla en pantalla no necesita y una hoja de cálculo
# sí — el código para cruzar con otros informes, las fechas para ordenar, y el contacto para
# actuar sin volver al panel.
CONSULTATION_COLUMNS: tuple[Column, ...] = (
    Column("status", "Estado", 22),
    Column("doctor", "Médico asignado", 26),
    Column("patient", "Paciente", 26),
    Column("elapsed", "Tiempo en progreso", 18),
    # El mismo dato como número. La etiqueta de arriba se lee, pero no se ordena ni se filtra:
    # "4 horas" y "40 min" no se comparan como texto, y lo primero que hace cualquiera con este
    # informe es buscar los casos que llevan más tiempo abiertos.
    Column("elapsed_hours", "Horas en progreso", 16),
    Column("chief_complaint", "Motivo de consulta", 60),
    Column("code", "Código", 16),
    Column("priority", "Prioridad", 12),
    Column("specialty", "Especialidad solicitada", 24),
    Column("patient_phone", "Teléfono del paciente", 20),
    Column("patient_zone", "Zona", 20),
    Column("queued_at", "Entró a la cola", 18, "datetime"),
    Column("opened_at", "Abierta", 18, "datetime"),
    Column("closed_at", "Cerrada", 18, "datetime"),
    Column("contacted", "Contactado", 12),
    Column("admin_follow_up", "Admin de seguimiento", 24),
    Column("nota_admin", "Nota del admin", 40),
    Column("consultation_id", "ID consulta", 38),
)


# --- Filtros ------------------------------------------------------------------


@dataclass(frozen=True)
class DoctorFilters:
    """Filtros del reporte de médicos.

    Los cinco primeros son EXACTAMENTE los de `GET /doctors` (se aplican con la misma función);
    los otros cuatro son los que el listado admin no tiene y un informe sí necesita.
    """

    status: int | None = None
    verified: bool | None = None
    can_practice: bool | None = None
    blocked_reason: str | None = None
    search: str | None = None
    specialty_id: uuid.UUID | None = None
    professional_type_id: uuid.UUID | None = None
    created_from: date | None = None
    created_to: date | None = None


@dataclass(frozen=True)
class PatientFilters:
    """Filtros del reporte de pacientes."""

    search: str | None = None
    origin: str | None = None  # "publica" | "consultorio"
    affected_zone: str | None = None
    age_range: str | None = None
    need_tag: str | None = None
    has_account: bool | None = None
    has_consultations: bool | None = None
    consent: bool | None = None
    include_archived: bool = False
    created_from: date | None = None
    created_to: date | None = None


@dataclass(frozen=True)
class ConsultationFilters:
    """Filtros del reporte de consultas.

    `statuses` vacío = todos los estados. El frontend manda el set del monitor por defecto, así
    que el informe sale siendo exactamente esa tabla y desde ahí se puede ampliar.
    """

    statuses: tuple[str, ...] = ()
    assigned_doctor_id: uuid.UUID | None = None
    specialty_id: uuid.UUID | None = None
    unassigned: bool | None = None
    search: str | None = None
    created_from: date | None = None
    created_to: date | None = None


def _describe(pairs: list[tuple[str, object]]) -> list[tuple[str, str]]:
    """Deja solo los filtros realmente aplicados. Listar los vacíos como "Todos" llenaría la
    portada de ruido y escondería los dos que de verdad acotan el informe."""
    return [(label, str(value)) for label, value in pairs if value not in (None, "", [])]


def describe_doctor_filters(
    f: DoctorFilters,
    *,
    specialty_name: str | None = None,
    professional_type_name: str | None = None,
) -> list[tuple[str, str]]:
    """Los filtros aplicados, legibles, para la portada del Excel.

    Los nombres de especialidad/tipo los resuelve el caller (tiene la sesión): escribir el UUID
    en la portada dejaría un informe que nadie puede auditar sin abrir la base.
    """
    return _describe(
        [
            ("Estado de la ficha", DOCTOR_STATUS_LABELS.get(f.status)),
            ("Credencial verificada", _si_no(f.verified, unknown="")),
            ("Habilitado para atender", _si_no(f.can_practice, unknown="")),
            ("Motivo de bloqueo", BLOCKED_REASON_LABELS.get(f.blocked_reason or "")),
            ("Búsqueda (nombre/cédula/email)", f.search),
            ("Especialidad", specialty_name),
            ("Tipo profesional", professional_type_name),
            ("Registrados desde", f.created_from),
            ("Registrados hasta", f.created_to),
        ]
    )


def describe_patient_filters(f: PatientFilters) -> list[tuple[str, str]]:
    """Los filtros aplicados, legibles, para la portada del Excel."""
    origins = {"publica": "Cola pública", "consultorio": "Consultorio (alta por médico)"}
    return _describe(
        [
            ("Búsqueda (nombre/cédula/email/teléfono)", f.search),
            ("Origen", origins.get(f.origin or "")),
            ("Zona afectada", f.affected_zone),
            ("Rango de edad", f.age_range),
            ("Necesidad", f.need_tag),
            ("Tiene cuenta", _si_no(f.has_account, unknown="")),
            ("Tiene consultas", _si_no(f.has_consultations, unknown="")),
            ("Consentimiento", _si_no(f.consent, unknown="")),
            ("Incluye archivados", "Sí" if f.include_archived else "No"),
            ("Registrados desde", f.created_from),
            ("Registrados hasta", f.created_to),
        ]
    )


# --- Consulta: médicos --------------------------------------------------------


def _doctor_consultation_stats():
    """Consultas por médico (una pasada agrupada, no una consulta por fila).

    Se agrupa por `assigned_doctor_id` (el id de la CUENTA), que es la columna con la que
    `consultations` referencia al médico — no por el id de la ficha en `doctors`.
    """
    return (
        select(
            Consultation.assigned_doctor_id.label("user_id"),
            func.count().label("total"),
            func.count().filter(Consultation.status.in_(CLOSED_STATUSES)).label("closed"),
            func.max(Consultation.created_at).label("last_at"),
        )
        .where(Consultation.assigned_doctor_id.is_not(None))
        .group_by(Consultation.assigned_doctor_id)
        .subquery()
    )


def doctors_query(filters: DoctorFilters) -> Select:
    """Consulta del reporte de médicos: el universo del panel admin + los nombres resueltos y
    las cifras de actividad. ÚNICA definición de los filtros del reporte: la comparten la vista
    previa y la exportación."""
    sub, blocked = admin_universe()
    spec = aliased(Specialty)
    ptype = aliased(ProfessionalType)
    account = aliased(Profile)
    stats = _doctor_consultation_stats()

    stmt = (
        select(
            sub,
            blocked,
            spec.name.label("specialty_name"),
            ptype.name.label("professional_type_name"),
            account.role.label("account_role"),
            account.active.label("account_active"),
            account.last_seen_at.label("last_seen_at"),
            func.coalesce(stats.c.total, 0).label("consultations_total"),
            func.coalesce(stats.c.closed, 0).label("consultations_closed"),
            stats.c.last_at.label("last_consultation_at"),
        )
        .select_from(sub)
        .outerjoin(spec, and_(spec.id == sub.c.specialty_id, spec.deleted_at.is_(None)))
        .outerjoin(ptype, and_(ptype.id == sub.c.professional_type_id, ptype.deleted_at.is_(None)))
        .outerjoin(account, account.id == sub.c.user_id)
        .outerjoin(stats, stats.c.user_id == sub.c.user_id)
    )
    stmt = apply_doctor_filters(
        stmt,
        sub,
        blocked,
        status=filters.status,
        verified=filters.verified,
        can_practice=filters.can_practice,
        blocked_reason=filters.blocked_reason,
        search=filters.search,
    )
    if filters.specialty_id is not None:
        stmt = stmt.where(sub.c.specialty_id == filters.specialty_id)
    if filters.professional_type_id is not None:
        stmt = stmt.where(sub.c.professional_type_id == filters.professional_type_id)
    start, end = day_bounds(filters.created_from, filters.created_to)
    if start is not None:
        stmt = stmt.where(sub.c.created_at >= start)
    if end is not None:
        stmt = stmt.where(sub.c.created_at < end)
    # Mismo orden que el listado admin (recientes primero), con `row_key` de desempate para que
    # dos fichas del mismo instante no bailen entre la vista previa y el archivo.
    return stmt.order_by(sub.c.created_at.desc(), sub.c.row_key)


def _doctor_row(r) -> dict:
    """Una fila del reporte de médicos, ya presentada (etiquetas, Sí/No, hora de Caracas)."""
    return {
        "full_name": r.full_name,
        "cedula": r.cedula,
        "license": r.license,
        "email": r.email,
        "phone": r.phone,
        "professional_type": r.professional_type_name,
        "specialty": r.specialty_name,
        "country": r.country,
        # Sin ficha no hay estado que reportar: `status` viene NULL y "De baja" sería mentira.
        "status": DOCTOR_STATUS_LABELS.get(r.status, "Sin ficha") if r.has_record else "Sin ficha",
        "verified": _si_no(r.verified),
        "can_practice": _si_no(r.blocked_reason is None),
        "blocked_reason": BLOCKED_REASON_LABELS.get(r.blocked_reason or "", ""),
        "has_account": _si_no(r.user_id is not None),
        "account_role": r.account_role,
        "account_active": _si_no(r.account_active),
        "last_seen_at": to_local(r.last_seen_at),
        "consultations_total": r.consultations_total,
        "consultations_closed": r.consultations_closed,
        "last_consultation_at": to_local(r.last_consultation_at),
        "created_at": to_local(r.created_at),
        "doctor_id": str(r.id) if r.id else "",
        "user_id": str(r.user_id) if r.user_id else "",
    }


# --- Consulta: pacientes ------------------------------------------------------


def _patient_consultation_stats():
    """Consultas por paciente: total, cerradas y fecha de la última (una pasada agrupada)."""
    return (
        select(
            Consultation.patient_id.label("patient_id"),
            func.count().label("total"),
            func.count().filter(Consultation.status.in_(CLOSED_STATUSES)).label("closed"),
            func.max(Consultation.created_at).label("last_at"),
        )
        .group_by(Consultation.patient_id)
        .subquery()
    )


def _patient_last_consultation():
    """Última consulta de cada paciente (código y estado), vía `DISTINCT ON` de Postgres.

    El agregado de arriba da la FECHA de la última, pero no de qué fila salió, y el estado del
    último caso es justo lo que hace accionable el informe ("¿este paciente sigue esperando?").
    `DISTINCT ON` lo resuelve en una pasada; el equivalente portable sería una ventana + filtro.
    """
    return (
        select(
            Consultation.patient_id.label("patient_id"),
            Consultation.code.label("code"),
            Consultation.status.label("status"),
        )
        .distinct(Consultation.patient_id)
        .order_by(Consultation.patient_id, Consultation.created_at.desc(), Consultation.id)
        .subquery()
    )


def patients_query(filters: PatientFilters) -> Select:
    """Consulta del reporte de pacientes: la ficha + quién lo registró, su adulto responsable y
    la actividad de sus casos. ÚNICA definición de los filtros (vista previa + exportación)."""
    doctor = aliased(Profile)
    parent = aliased(Patient)
    stats = _patient_consultation_stats()
    last = _patient_last_consultation()

    stmt = (
        select(
            Patient,
            doctor.full_name.label("registered_by"),
            parent.full_name.label("parent_name"),
            func.coalesce(stats.c.total, 0).label("consultations_total"),
            func.coalesce(stats.c.closed, 0).label("consultations_closed"),
            stats.c.last_at.label("last_consultation_at"),
            last.c.code.label("last_consultation_code"),
            last.c.status.label("last_consultation_status"),
        )
        .select_from(Patient)
        .outerjoin(doctor, doctor.id == Patient.created_by_doctor_id)
        .outerjoin(parent, parent.id == Patient.parent_id)
        .outerjoin(stats, stats.c.patient_id == Patient.id)
        .outerjoin(last, last.c.patient_id == Patient.id)
    )

    # El archivado (soft delete) queda fuera salvo que se pida: mismo criterio que
    # `patients.list_patients`, y un informe que los cuenta sin decirlo infla los totales.
    if not filters.include_archived:
        stmt = stmt.where(Patient.deleted_at.is_(None))
    if filters.origin == "publica":
        stmt = stmt.where(Patient.created_by_doctor_id.is_(None))
    elif filters.origin == "consultorio":
        stmt = stmt.where(Patient.created_by_doctor_id.is_not(None))
    if filters.affected_zone:
        stmt = stmt.where(Patient.affected_zone == filters.affected_zone)
    if filters.age_range:
        stmt = stmt.where(Patient.age_range == filters.age_range)
    if filters.need_tag:
        stmt = stmt.where(Patient.needs_tags.any(filters.need_tag))
    if filters.has_account is not None:
        stmt = stmt.where(
            Patient.user_id.is_not(None) if filters.has_account else Patient.user_id.is_(None)
        )
    if filters.consent is not None:
        stmt = stmt.where(Patient.consent.is_(filters.consent))
    if filters.has_consultations is not None:
        stmt = stmt.where(
            stats.c.total.is_not(None) if filters.has_consultations else stats.c.total.is_(None)
        )
    if filters.search and (term := filters.search.strip()):
        like = f"%{term}%"
        stmt = stmt.where(
            or_(
                Patient.full_name.ilike(like),
                Patient.cedula.ilike(like),
                Patient.email.ilike(like),
                Patient.phone_whatsapp.ilike(like),
            )
        )
    start, end = day_bounds(filters.created_from, filters.created_to)
    if start is not None:
        stmt = stmt.where(Patient.created_at >= start)
    if end is not None:
        stmt = stmt.where(Patient.created_at < end)
    return stmt.order_by(Patient.created_at.desc(), Patient.id)


def _patient_row(r) -> dict:
    """Una fila del reporte de pacientes, ya presentada."""
    p: Patient = r[0]
    return {
        "full_name": p.full_name,
        "cedula": p.cedula,
        "age_range": p.age_range,
        "phone_whatsapp": p.phone_whatsapp,
        "email": p.email,
        "affected_zone": p.affected_zone,
        "needs_tags": ", ".join(p.needs_tags or []),
        "description": p.description,
        "allergies": p.allergies,
        "origin": "Consultorio" if p.created_by_doctor_id else "Cola pública",
        "registered_by": r.registered_by,
        "consent": _si_no(p.consent),
        "consent_at": to_local(p.consent_at),
        "has_account": _si_no(p.user_id is not None),
        "parent_name": r.parent_name,
        "parentesco": p.parentesco,
        "consultations_total": r.consultations_total,
        "consultations_closed": r.consultations_closed,
        "last_consultation_at": to_local(r.last_consultation_at),
        "last_consultation_status": STATUS_LABELS.get(
            r.last_consultation_status or "", r.last_consultation_status or ""
        ),
        "last_consultation_code": r.last_consultation_code,
        "archived": _si_no(p.deleted_at is not None),
        "created_at": to_local(p.created_at),
        "patient_id": str(p.id),
    }


# --- Consulta: consultas ------------------------------------------------------


def _elapsed(since: datetime | None, until: datetime | None) -> timedelta | None:
    """Cuánto lleva (o llevó) el caso desde que un médico lo tomó.

    `until` es `closed_at` cuando la consulta ya cerró y "ahora" cuando sigue abierta. Medir
    siempre contra ahora daría, para un caso cerrado hace un mes, un "30 días en progreso" que
    no significa nada. Para las del monitor —que son justo las que no han cerrado— el resultado
    es idéntico al que pinta la pantalla.
    """
    if since is None:
        return None
    base = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
    fin = until or datetime.now(UTC)
    fin = fin if fin.tzinfo is not None else fin.replace(tzinfo=UTC)
    return fin - base


def _elapsed_label(delta: timedelta | None) -> str:
    """El mismo texto que pinta el modal (`lib/utils.ts::tiempoTranscurrido`): minutos por
    debajo de la hora, horas por debajo del día, y días + horas por encima. Se replica en vez de
    inventar otro formato para que la columna del Excel diga lo mismo que la pantalla."""
    if delta is None:
        return ""
    total_min = max(int(delta.total_seconds() // 60), 0)
    if total_min < 60:
        return f"{total_min} min"
    total_horas = total_min // 60
    if total_horas < 24:
        return f"{total_horas} {'hora' if total_horas == 1 else 'horas'}"
    dias, horas = divmod(total_horas, 24)
    etiqueta = f"{dias} {'día' if dias == 1 else 'días'}"
    if horas == 0:
        return etiqueta
    return f"{etiqueta} {horas} {'hora' if horas == 1 else 'horas'}"


def consultations_query(filters: ConsultationFilters) -> Select:
    """Consulta del reporte de consultas: el caso + paciente, médico y especialidad resueltos.

    Una sola consulta con joins, no seis peticiones paginadas como hace el monitor del
    dashboard (que pide una por estado porque `GET /consultations` solo acepta un `status` a la
    vez, y se queda en 100 filas por estado). Aquí no hay tope por estado: el informe trae todo
    lo que cumple el filtro, así que puede tener MÁS filas que el modal del que salió.
    """
    doctor = aliased(Profile)
    admin = aliased(Profile)

    stmt = (
        select(
            Consultation,
            Patient.full_name.label("patient_name"),
            Patient.phone_whatsapp.label("patient_phone"),
            Patient.affected_zone.label("patient_zone"),
            doctor.full_name.label("doctor_name"),
            admin.full_name.label("admin_name"),
            Specialty.name.label("specialty_name"),
        )
        .select_from(Consultation)
        .outerjoin(Patient, Patient.id == Consultation.patient_id)
        .outerjoin(doctor, doctor.id == Consultation.assigned_doctor_id)
        .outerjoin(admin, admin.id == Consultation.admin_seguimiento)
        .outerjoin(Specialty, Specialty.id == Consultation.specialty_id)
    )

    if filters.statuses:
        stmt = stmt.where(Consultation.status.in_(filters.statuses))
    if filters.assigned_doctor_id is not None:
        stmt = stmt.where(Consultation.assigned_doctor_id == filters.assigned_doctor_id)
    if filters.specialty_id is not None:
        stmt = stmt.where(Consultation.specialty_id == filters.specialty_id)
    if filters.unassigned is not None:
        stmt = stmt.where(
            Consultation.assigned_doctor_id.is_(None)
            if filters.unassigned
            else Consultation.assigned_doctor_id.is_not(None)
        )
    if filters.search and (term := filters.search.strip()):
        like = f"%{term}%"
        stmt = stmt.where(
            or_(
                Patient.full_name.ilike(like),
                Consultation.code.ilike(like),
                Consultation.chief_complaint.ilike(like),
                doctor.full_name.ilike(like),
            )
        )
    start, end = day_bounds(filters.created_from, filters.created_to)
    if start is not None:
        stmt = stmt.where(Consultation.created_at >= start)
    if end is not None:
        stmt = stmt.where(Consultation.created_at < end)
    # Las más antiguas primero: en un listado de casos abiertos, el que más tiempo lleva es el
    # que hay que mirar, y en un Excel la primera fila es la que se lee.
    return stmt.order_by(Consultation.created_at.asc(), Consultation.id)


def _consultation_row(r) -> dict:
    """Una fila del reporte de consultas, ya presentada."""
    c: Consultation = r[0]
    desde = c.opened_at or c.started_at or c.queued_at
    delta = _elapsed(desde, c.closed_at)
    return {
        "status": STATUS_LABELS.get(c.status, c.status),
        # Mismo texto que la pantalla para el caso sin asignar: quien compare el Excel con el
        # panel no debería tener que traducir un hueco.
        "doctor": r.doctor_name or "— sin asignar —",
        "patient": r.patient_name,
        "elapsed": _elapsed_label(delta),
        "elapsed_hours": round(delta.total_seconds() / 3600, 1) if delta else None,
        "chief_complaint": c.chief_complaint,
        "code": c.code,
        "priority": c.priority,
        "specialty": r.specialty_name,
        "patient_phone": r.patient_phone,
        "patient_zone": r.patient_zone,
        "queued_at": to_local(c.queued_at),
        "opened_at": to_local(c.opened_at or c.started_at),
        "closed_at": to_local(c.closed_at),
        "contacted": _si_no(c.contacted),
        "admin_follow_up": r.admin_name,
        "nota_admin": c.nota_admin,
        "consultation_id": str(c.id),
    }


def describe_consultation_filters(
    f: ConsultationFilters,
    *,
    specialty_name: str | None = None,
    doctor_name: str | None = None,
) -> list[tuple[str, str]]:
    """Los filtros aplicados, legibles, para la portada del Excel."""
    if not f.statuses:
        estados = None
    elif tuple(f.statuses) == IN_PROGRESS_STATUSES:
        # El caso normal merece su nombre: "En progreso" dice de un vistazo de qué informe se
        # trata, mientras que los seis estados enumerados obligan a reconstruirlo.
        estados = "En progreso (derivadas, urgentes, no-show, canceladas y por WhatsApp)"
    else:
        estados = ", ".join(STATUS_LABELS.get(x, x) for x in f.statuses)
    return _describe(
        [
            ("Estados", estados),
            ("Médico asignado", doctor_name),
            ("Sin médico asignado", _si_no(f.unassigned, unknown="")),
            ("Especialidad", specialty_name),
            ("Búsqueda (paciente/código/motivo/médico)", f.search),
            ("Creadas desde", f.created_from),
            ("Creadas hasta", f.created_to),
        ]
    )


# --- Ejecución ----------------------------------------------------------------


async def _catalog_name(session: AsyncSession, model, item_id: uuid.UUID | None) -> str | None:
    """Nombre de una entrada de catálogo por id. Para que la portada diga "Pediatría" y no un
    UUID: un informe cuyos filtros solo se entienden abriendo la base no es auditable."""
    if item_id is None:
        return None
    return await session.scalar(select(model.name).where(model.id == item_id))


@dataclass
class Report:
    """Un reporte ya resuelto: filas presentadas, total que cumple el filtro y de qué filtros
    salió. `total` puede ser mayor que `len(rows)` en la vista previa (está paginada); en la
    exportación son iguales."""

    columns: tuple[Column, ...]
    rows: list[dict]
    total: int
    filters: list[tuple[str, str]] = field(default_factory=list)


async def _run(
    session: AsyncSession,
    stmt: Select,
    columns: tuple[Column, ...],
    to_row,
    described: list[tuple[str, str]],
    *,
    skip: int | None,
    limit: int | None,
) -> Report:
    """Cuenta el total y materializa la página pedida (o todo, si `limit` es None)."""
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    if skip:
        stmt = stmt.offset(skip)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = [to_row(r) for r in (await session.execute(stmt)).all()]
    return Report(columns=columns, rows=rows, total=total, filters=described)


async def doctors_report(
    session: AsyncSession,
    filters: DoctorFilters,
    *,
    skip: int | None = None,
    limit: int | None = None,
) -> Report:
    """Reporte de médicos. Sin `limit` devuelve la población completa (exportación).

    Los nombres de especialidad y tipo profesional se resuelven aquí, no en el caller: la lista
    de filtros aplicados tiene que ser la misma en la vista previa y en la portada del Excel.
    Resolviéndolos solo en la exportación, la vista previa se saltaba esos dos chips y quedaba
    enseñando un total recortado por un filtro que el usuario no veía en ninguna parte.
    """
    return await _run(
        session,
        doctors_query(filters),
        DOCTOR_COLUMNS,
        _doctor_row,
        describe_doctor_filters(
            filters,
            specialty_name=await _catalog_name(session, Specialty, filters.specialty_id),
            professional_type_name=await _catalog_name(
                session, ProfessionalType, filters.professional_type_id
            ),
        ),
        skip=skip,
        limit=limit,
    )


async def patients_report(
    session: AsyncSession,
    filters: PatientFilters,
    *,
    skip: int | None = None,
    limit: int | None = None,
) -> Report:
    """Reporte de pacientes. Sin `limit` devuelve la población completa (exportación)."""
    return await _run(
        session,
        patients_query(filters),
        PATIENT_COLUMNS,
        _patient_row,
        describe_patient_filters(filters),
        skip=skip,
        limit=limit,
    )


async def consultations_report(
    session: AsyncSession,
    filters: ConsultationFilters,
    *,
    skip: int | None = None,
    limit: int | None = None,
) -> Report:
    """Reporte de consultas. Sin `limit` devuelve la población completa (exportación)."""
    doctor_name = None
    if filters.assigned_doctor_id is not None:
        doctor_name = await session.scalar(
            select(Profile.full_name).where(Profile.id == filters.assigned_doctor_id)
        )
    return await _run(
        session,
        consultations_query(filters),
        CONSULTATION_COLUMNS,
        _consultation_row,
        describe_consultation_filters(
            filters,
            specialty_name=await _catalog_name(session, Specialty, filters.specialty_id),
            doctor_name=doctor_name,
        ),
        skip=skip,
        limit=limit,
    )


# --- Excel --------------------------------------------------------------------


def build_workbook(report: Report, *, title: str, sheet_name: str, generated_by: str) -> bytes:
    """Renderiza el reporte como un .xlsx en memoria y devuelve sus bytes.

    Dos hojas a propósito: la de datos, y una portada con los filtros aplicados, quién exportó
    y cuándo. Un listado de 3000 filas sin decir de qué filtro salió no es un informe, es un
    volcado — y a la semana nadie sabe si incluía a los médicos de baja.

    `constant_memory` escribe fila a fila al archivo en vez de retener la hoja entera: con él un
    export grande no depende de que quepa en RAM. A cambio las filas deben escribirse en orden
    (lo están) y el ancho de columna hay que fijarlo antes de escribirlas (también).
    """
    buffer = BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True, "constant_memory": True})
    header_fmt = book.add_format(
        {"bold": True, "bg_color": "#0f172a", "font_color": "#ffffff", "border": 1}
    )
    date_fmt = book.add_format({"num_format": "dd/mm/yyyy hh:mm"})
    title_fmt = book.add_format({"bold": True, "font_size": 14})
    label_fmt = book.add_format({"bold": True})

    cover = book.add_worksheet("Reporte")
    cover.set_column(0, 0, 34)
    cover.set_column(1, 1, 60)
    cover.write(0, 0, title, title_fmt)
    generated = to_local(datetime.now(UTC))
    row = 2
    for label, value in (
        ("Generado", f"{generated:%d/%m/%Y %H:%M} (hora de Venezuela)"),
        ("Generado por", generated_by),
        ("Filas", str(report.total)),
    ):
        cover.write(row, 0, label, label_fmt)
        cover.write(row, 1, value)
        row += 1
    row += 1
    cover.write(row, 0, "Filtros aplicados", label_fmt)
    row += 1
    if not report.filters:
        cover.write(row, 0, "Sin filtros: todos los registros")
    for label, value in report.filters:
        cover.write(row, 0, label, label_fmt)
        cover.write(row, 1, value)
        row += 1

    sheet = book.add_worksheet(sheet_name)
    sheet.freeze_panes(1, 0)
    # El autofiltro cubre al menos la fila de cabecera: con 0 filas de datos, un rango vacío
    # deja el archivo con un filtro que Excel marca como corrupto.
    sheet.autofilter(0, 0, max(len(report.rows), 1), len(report.columns) - 1)
    for index, column in enumerate(report.columns):
        sheet.set_column(index, index, column.width)
        sheet.write(0, index, column.header, header_fmt)
    for r, data in enumerate(report.rows, start=1):
        for c, column in enumerate(report.columns):
            value = data.get(column.key)
            if value is None or value == "":
                continue
            if column.kind == "datetime":
                sheet.write_datetime(r, c, value, date_fmt)
            else:
                sheet.write(r, c, value)

    book.close()
    return buffer.getvalue()


# --- Exportación --------------------------------------------------------------


def _guard_size(total: int) -> None:
    """Rechaza exportaciones desmedidas ANTES de materializar las filas.

    El tope no protege del tamaño del archivo, sino del proceso: construir cientos de miles de
    filas en un worker deja sin memoria a la API entera, y el síntoma le llega a los médicos
    que están atendiendo, no a quien pulsó "Exportar". Falla con un 422 que dice qué hacer.
    """
    if total > MAX_EXPORT_ROWS:
        raise UnprocessableError(
            f"El filtro seleccionado devuelve {total} filas y el máximo por exportación es "
            f"{MAX_EXPORT_ROWS}. Acota el reporte (por fecha, estado o búsqueda) y vuelve a "
            "intentarlo."
        )


async def _log_export(
    session: AsyncSession, *, report_name: str, actor_user_id: uuid.UUID, report: Report
) -> None:
    """Deja en `audit_log` que este usuario extrajo este reporte, con su filtro y su tamaño.

    Es el registro de una extracción masiva de PII médica a un archivo que sale de la
    plataforma: sin él, un volcado de 3000 pacientes es indistinguible de no haber pasado nada.
    Se guarda el filtro aplicado, no las filas — el audit no debe contener la PII que audita.
    """
    await audit.log_action(
        session,
        action="report.exported",
        actor_user_id=actor_user_id,
        resource="reports",
        resource_id=report_name,
        metadata={
            "report": report_name,
            "rows": report.total,
            "filters": {label: value for label, value in report.filters},
        },
    )
    # Commit propio: el endpoint es de lectura y no hay transacción del caller a la que
    # engancharse, así que sin esto la entrada del audit se iría con el rollback de la sesión.
    await session.commit()


async def export_doctors(
    session: AsyncSession,
    filters: DoctorFilters,
    *,
    actor_user_id: uuid.UUID,
    actor_label: str,
) -> bytes:
    """El `.xlsx` de médicos que cumplen el filtro (población completa) + su entrada de audit."""
    # `limit=MAX_EXPORT_ROWS + 1`: se cuenta el total antes de traer filas, así que el guard
    # corta con el `total` exacto; el limit es solo el cinturón por si la cuenta y la página
    # divergieran (datos escritos entre ambas consultas).
    report = await doctors_report(session, filters, limit=MAX_EXPORT_ROWS + 1)
    _guard_size(report.total)
    await _log_export(session, report_name="doctors", actor_user_id=actor_user_id, report=report)
    return build_workbook(
        report, title="Reporte de médicos", sheet_name="Médicos", generated_by=actor_label
    )


async def export_patients(
    session: AsyncSession,
    filters: PatientFilters,
    *,
    actor_user_id: uuid.UUID,
    actor_label: str,
) -> bytes:
    """El `.xlsx` de pacientes que cumplen el filtro (población completa) + su entrada de audit."""
    report = await patients_report(session, filters, limit=MAX_EXPORT_ROWS + 1)
    _guard_size(report.total)
    await _log_export(session, report_name="patients", actor_user_id=actor_user_id, report=report)
    return build_workbook(
        report, title="Reporte de pacientes", sheet_name="Pacientes", generated_by=actor_label
    )


async def export_consultations(
    session: AsyncSession,
    filters: ConsultationFilters,
    *,
    actor_user_id: uuid.UUID,
    actor_label: str,
) -> bytes:
    """El `.xlsx` de consultas que cumplen el filtro (población completa) + su entrada de audit.

    Incluye el motivo de consulta, que es contenido clínico escrito por el paciente. Misma
    sensibilidad que el reporte de pacientes y el mismo gate."""
    report = await consultations_report(session, filters, limit=MAX_EXPORT_ROWS + 1)
    _guard_size(report.total)
    await _log_export(
        session, report_name="consultations", actor_user_id=actor_user_id, report=report
    )
    return build_workbook(
        report, title="Reporte de consultas", sheet_name="Consultas", generated_by=actor_label
    )
