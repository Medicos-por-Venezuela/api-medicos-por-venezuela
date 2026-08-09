"""Esquemas Pydantic para consultations (Create / Update / Response)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Re-exportado desde el modelo para tener una única fuente de verdad.
from src.models.consultation import CONSULTATION_STATUSES

__all__ = [
    "CONSULTATION_STATUSES",
    "ConsultationCreate",
    "ConsultationCreatedResponse",
    "ConsultationUpdate",
    "ConsultationResponse",
    "ConsultationDetailPatient",
    "ConsultationDetailResponse",
    "ConsultationPatientResponse",
    "ConsultationCloseRequest",
    "ConsultationClaimRequest",
    "ScheduleFollowUpRequest",
    "ScheduleReferralRequest",
    "ReminderRunResponse",
    "ChainItem",
    "ConsultationPanelResponse",
    "PanelConsultationItem",
    "PanelPatient",
    "PanelWaitingItem",
    "PanelWaitingPatient",
    "QueueReleaseResponse",
]


class ConsultationBase(BaseModel):
    """Base usada SOLO por `ConsultationResponse` (esquema de salida).

    Sin `max_length`: son datos ya persistidos en la base y una validación de
    salida no debe rechazar filas existentes que excedan un límite pensado para
    entrada (ver ConsultationCreate/ConsultationUpdate, que sí lo validan).
    """

    patient_id: uuid.UUID
    priority: str = "normal"
    category: str | None = None
    # Especialidad solicitada por el paciente (catálogo specialties). Reemplaza a
    # needs_tags para el registro nuevo; el filtro del panel se actualiza aparte.
    specialty_id: uuid.UUID | None = None
    chief_complaint: str | None = None
    referred_specialty: str | None = None
    doctor_id: uuid.UUID | None = None
    assigned_doctor_id: uuid.UUID | None = None
    platform_used: str | None = None
    meeting_link: str | None = None
    video_room_url: str | None = None


class ConsultationCreate(BaseModel):
    """Entrada pública para crear una consulta.

    Solo acepta los campos que el cliente puede fijar legítimamente.
    Campos server-only excluidos deliberadamente: `assigned_doctor_id`, `doctor_id`,
    `video_room_url`, `meeting_link` (se asignan por el backend/cola, no por el cliente).
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: uuid.UUID
    priority: str = Field("normal", max_length=20)
    category: str | None = Field(default=None, max_length=100)
    # OBLIGATORIO: `consultations.specialty_id` ES el matching de la cola. Cuando era opcional
    # entraban filas sin especialidad y el backend caía a un mapa de nombres hardcodeado que se
    # desincronizaba del catálogo; ese mapa se eliminó y las filas históricas se rellenaron con
    # una migración, así que la columna no puede volver a quedar vacía por la puerta de entrada.
    specialty_id: uuid.UUID
    chief_complaint: str | None = Field(default=None, max_length=500)
    referred_specialty: str | None = Field(default=None, max_length=100)
    platform_used: str | None = Field(default=None, max_length=50)
    # El code lo asigna SIEMPRE el trigger generate_consultation_code en la base.
    status: str = "waiting"


class ConsultationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = Field(default=None, max_length=30)
    priority: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=100)
    specialty_id: uuid.UUID | None = None
    chief_complaint: str | None = Field(default=None, max_length=500)
    clinical_notes: str | None = Field(default=None, max_length=5000)
    internal_note: str | None = Field(default=None, max_length=2000)
    doctor_id: uuid.UUID | None = None
    assigned_doctor_id: uuid.UUID | None = None
    # Gestión del admin (panel admin/pacientes): asignar super_admin de seguimiento / nota libre.
    admin_seguimiento: uuid.UUID | None = None
    nota_admin: str | None = Field(default=None, max_length=5000)
    referred_specialty: str | None = Field(default=None, max_length=100)
    platform_used: str | None = Field(default=None, max_length=50)
    meeting_link: str | None = Field(default=None, max_length=500)
    video_room_url: str | None = Field(default=None, max_length=500)
    contacted: bool | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None


class ConsultationCloseRequest(BaseModel):
    """Cierre de consulta: `closed` (completada) o `patient_no_show` (ausencia).

    El autor del cierre se toma del JWT (no se acepta del cliente).
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["closed", "patient_no_show"] = "closed"
    note: str | None = Field(default=None, max_length=2000)
    # Firma del médico (dataURL PNG). Se persiste como acto médico firmado (base para récipes).
    signature: str | None = Field(default=None, max_length=2_000_000)


class ScheduleFollowUpRequest(BaseModel):
    """Agendar seguimiento: cierra la consulta actual (firmada) y crea una consulta HIJA agendada
    para otra fecha (misma cadena). Ver el módulo Agenda."""

    model_config = ConfigDict(extra="forbid")

    scheduled_at: datetime
    closing_note: str | None = Field(default=None, max_length=2000)
    signature: str | None = Field(default=None, max_length=2_000_000)


class ScheduleReferralRequest(BaseModel):
    """Agendar con especialista (referencia): entrega la consulta al médico invitado — la actual
    queda 'referred_to_specialist' — y crea una HIJA agendada asignada a ESE médico, con el motivo
    firmado. El especialista ve las notas previas (chain). Distinto de 'Agendar seguimiento'."""

    model_config = ConfigDict(extra="forbid")

    invited_doctor_id: uuid.UUID
    scheduled_at: datetime
    reason: str = Field(min_length=1, max_length=2000)  # por qué se refiere
    signature: str | None = Field(default=None, max_length=2_000_000)


class ChainItem(BaseModel):
    """Un eslabón de la cadena de seguimiento (historial cross-consulta padre→hijas)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: str
    chief_complaint: str | None = None
    internal_note: str | None = None
    scheduled_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime
    parent_consultation_id: uuid.UUID | None = None


class QueueReleaseResponse(BaseModel):
    """Resultado de liberar consultas estancadas."""

    released: int
    threshold_minutes: int


class ReminderRunResponse(BaseModel):
    """Resultado de correr los recordatorios de citas (para el cron externo)."""

    sent: int
    window_minutes: int


class ConsultationDetailPatient(BaseModel):
    """Paciente anidado en el DETALLE de una consulta (GET /{id}), para el médico que la atiende
    (no la cola). Así el frontend no lee la tabla `patients` directo. Solo lo recibe staff
    autorizado a ver el caso."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    cedula: str | None = None
    phone_whatsapp: str | None = None
    email: str | None = None
    affected_zone: str | None = None
    age_range: str | None = None
    needs_tags: list[str] | None = None
    description: str | None = None


class ConsultationResponse(ConsultationBase):
    """Vista completa para staff (incluye notas clínicas e internas)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: str
    clinical_notes: str | None = None
    internal_note: str | None = None
    doctor_license_snapshot: dict | None = None
    has_prescription: bool
    has_referral: bool
    has_rest_note: bool
    follow_up_scheduled: bool
    contacted: bool
    attended_via_whatsapp: bool
    queued_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    entered_call_at: datetime | None = None
    patient_last_seen_at: datetime | None = None
    created_at: datetime
    # Agenda / cadena de seguimiento.
    scheduled_at: datetime | None = None
    parent_consultation_id: uuid.UUID | None = None
    # Enriquecimiento para el panel admin (monitor de consultas): nombres resueltos
    # server-side vía join (patients.full_name / users.full_name por
    # assigned_doctor_id). Opcionales: nulos si el servicio no los resuelve o la
    # consulta está sin asignar.
    patient_name: str | None = None
    assigned_doctor_name: str | None = None
    # Gestión del admin (panel admin/pacientes): super_admin de seguimiento + nota libre.
    admin_seguimiento: uuid.UUID | None = None
    nota_admin: str | None = None


class ConsultationDetailResponse(ConsultationResponse):
    """Detalle de una consulta para el médico que la atiende: la vista de staff + el paciente
    anidado. SOLO para GET /{id}; los listados usan ConsultationResponse (que no toca la relación
    `patient`, para no dispararla en lazy-load). El router puebla `patient` explícitamente."""

    patient: ConsultationDetailPatient | None = None


class ConsultationCreatedResponse(ConsultationResponse):
    """Respuesta de POST /consultations: la consulta MÁS el token de acceso a su sala.

    El token se entrega aquí y solo aquí — es la única vez que el paciente anónimo puede
    recibirlo, porque no tiene sesión con la que volver a pedirlo. El frontend lo lleva en la
    URL de /sala-espera en lugar del `cid` crudo. Deliberadamente NO va en
    `ConsultationResponse`: los listados del panel no deben repartir credenciales de sala.
    """

    access_token: str


class ConsultationPatientResponse(BaseModel):
    """Vista reducida para pacientes autenticados.

    Excluye deliberadamente `internal_note`, `clinical_notes` y
    `doctor_license_snapshot`: son campos de uso interno del staff
    que no deben ser visibles al paciente (equivalente a la RLS de Supabase).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    patient_id: uuid.UUID
    status: str
    priority: str
    category: str | None = None
    specialty_id: uuid.UUID | None = None
    chief_complaint: str | None = None
    referred_specialty: str | None = None
    platform_used: str | None = None
    video_room_url: str | None = None
    has_prescription: bool
    has_referral: bool
    has_rest_note: bool
    follow_up_scheduled: bool
    contacted: bool
    queued_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    patient_last_seen_at: datetime | None = None
    created_at: datetime
    # Cita agendada (módulo Agenda): el portal del paciente (mi-caso) lista sus próximas citas.
    scheduled_at: datetime | None = None


class ConsultationClaimRequest(BaseModel):
    """Cuerpo para tomar una consulta desde el panel médico."""

    model_config = ConfigDict(extra="forbid")

    # true = atención por WhatsApp (sin videollamada); false = se abre la sala de video.
    via_whatsapp: bool = False


class PanelWaitingPatient(BaseModel):
    """Paciente en la COLA DE ESPERA (sin asignar): SIN nombre. Hasta que el médico toma la
    consulta no se expone el nombre del paciente — ni en la UI ni en la respuesta del endpoint —
    por seguridad. El médico elige el caso por síntomas/zona, no por nombre."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cedula: str | None = None
    phone_whatsapp: str | None = None
    affected_zone: str | None = None
    age_range: str | None = None
    needs_tags: list[str] | None = None
    description: str | None = None
    # Las alergias se piden en el registro y son dato clínico de decisión: el médico las
    # necesita ANTES de tomar el caso, no después. Van sin nombre, como el resto de la fila.
    allergies: str | None = None


class PanelPatient(PanelWaitingPatient):
    """Paciente de una consulta YA tomada por el médico (mis consultas abiertas): incluye el
    nombre, porque el caso ya está siendo atendido."""

    full_name: str


class PanelConsultationItem(BaseModel):
    """Fila de consulta para las listas del panel médico (cola de espera y las propias),
    con el paciente anidado. Excluye notas clínicas/internas (no se muestran en la cola)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: str
    priority: str
    category: str | None = None
    # Nombre de la especialidad solicitada (specialty_id resuelta): con esto matchea el médico.
    specialty: str | None = None
    chief_complaint: str | None = None
    referred_specialty: str | None = None
    video_room_url: str | None = None
    assigned_doctor_id: uuid.UUID | None = None
    attended_via_whatsapp: bool
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    patient_last_seen_at: datetime | None = None
    created_at: datetime
    patient: PanelPatient | None = None


class PanelWaitingItem(PanelConsultationItem):
    """Fila de la COLA DE ESPERA: igual que PanelConsultationItem pero con el paciente SIN nombre
    (PanelWaitingPatient). Al tomar la consulta pasa a `mine` y ahí sí se muestra con nombre."""

    patient: PanelWaitingPatient | None = None


class ConsultationPanelResponse(BaseModel):
    """Payload del panel médico en una sola llamada: cola de espera sin asignar (paciente SIN
    nombre, por seguridad), las consultas abiertas del propio médico (con nombre) y cuántas ha
    cerrado."""

    waiting: list[PanelWaitingItem]
    mine: list[PanelConsultationItem]
    my_closed_count: int
