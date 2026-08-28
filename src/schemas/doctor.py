"""Esquemas Pydantic para doctors (Create / Update / Response).

Los patrones de `cedula` y `phone` reflejan los CHECK de la tabla; la cédula se
normaliza a mayúscula (V-/E-) para casar con el índice único y el CHECK.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Cédula venezolana: V-12345678 / E-12345678 (acepta minúscula, se normaliza).
_CEDULA_PATTERN = r"^[VEve]-\d{6,9}$"
# Teléfono internacional: +<prefijo><número>, p. ej. +5804145200715.
_PHONE_PATTERN = r"^\+\d{7,15}$"


class DoctorCreate(BaseModel):
    """Registro de un médico. `status`/`verified` los fija el backend, no el cliente."""

    model_config = ConfigDict(extra="forbid")

    professional_type_id: uuid.UUID
    specialty_id: uuid.UUID | None = None
    cedula: str = Field(..., pattern=_CEDULA_PATTERN)
    full_name: str = Field(..., min_length=2, max_length=200)
    license: str | None = Field(default=None, max_length=100)
    phone: str = Field(..., pattern=_PHONE_PATTERN)
    email: EmailStr
    country_of_residence: str | None = Field(default=None, max_length=100)
    # Honeypot anti-bot: debe llegar vacío. El frontend lo renderiza oculto; un
    # humano no lo llena. Si viene con valor, el backend rechaza la solicitud.
    website: str | None = Field(default=None, max_length=200)

    @field_validator("cedula")
    @classmethod
    def _normalize_cedula(cls, value: str) -> str:
        return value.upper()


class DoctorUpdate(BaseModel):
    """Edición administrativa de la ficha. Permite mover `status` (0/1/2).

    NO incluye `verified`: habilitar a un médico que el SACS/FPV no validó es una acción
    con nombre propio (`POST /doctors/{id}/approve`, permiso `doctors.verify`) porque tiene
    que quedar distinguible en `audit_log` de un cambio de teléfono. Si se pudiera colar por
    aquí, esa traza sería opcional.
    """

    model_config = ConfigDict(extra="forbid")

    specialty_id: uuid.UUID | None = None
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    license: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, pattern=_PHONE_PATTERN)
    email: EmailStr | None = None
    country_of_residence: str | None = Field(default=None, max_length=100)
    status: int | None = Field(default=None, ge=0, le=2)


class DoctorSelfUpdate(BaseModel):
    """Auto-edición del médico sobre su **propio** perfil (campos del wireframe:
    cédula, nombre, licencia, especialidad).

    A diferencia de `DoctorUpdate` (admin), NO permite `status`/`verified`/`email`/
    `phone`: un médico no puede autoverificarse, reactivarse ni cambiar el contacto
    que liga la cuenta. Cambiar la `cedula` re-dispara la verificación SACS/FPV y
    recalcula `verified` (solo aplica cuando existe fila en `doctors`).

    `professional_type_id` solo se usa cuando una cuenta **sin ficha** (`source:"user"`,
    médico que entró por Google) completa su registro: junto con `cedula` elige el
    registro oficial (SACS/FPV) contra el que verificar y **crea** la fila en `doctors`.
    En una ficha ya existente (`source:"doctor"`) se ignora (el tipo no es auto-editable).
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    license: str | None = Field(default=None, max_length=100)
    specialty_id: uuid.UUID | None = None
    professional_type_id: uuid.UUID | None = None
    cedula: str | None = Field(default=None, pattern=_CEDULA_PATTERN)

    @field_validator("cedula")
    @classmethod
    def _normalize_cedula(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None


class DoctorMeResponse(BaseModel):
    """Perfil propio del médico, unificado sobre sus dos posibles fuentes: la fila
    en `doctors` (registro con verificación SACS/FPV) o, si no existe, la cuenta en
    `users` (médicos que entraron por Google/`finalize-role`). `source` indica cuál;
    en la fuente `user` no hay `cedula`, `specialty_id` ni `professional_type_id`
    (users guarda el nombre de la especialidad, no ids, y no conoce el tipo profesional).

    `professional_type_id`/`professional_type` (nombre, ej. "Médico"/"Psicólogo") permiten
    al frontend elegir el registro correcto (SACS vs FPV) para la verificación en vivo de
    la cédula. En `source:"user"` ambos vienen `null` hasta que el médico completa su ficha."""

    source: Literal["doctor", "user"]
    user_id: uuid.UUID
    doctor_id: uuid.UUID | None = None
    cedula: str | None = None
    full_name: str
    license: str | None = None
    specialty_id: uuid.UUID | None = None
    specialty: str | None = None
    professional_type_id: uuid.UUID | None = None
    professional_type: str | None = None
    verified: bool


class DoctorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    professional_type_id: uuid.UUID | None = None
    specialty_id: uuid.UUID | None = None
    # nullable: los médicos backfilleados/creados desde users no traen estos campos.
    cedula: str | None = None
    full_name: str
    license: str | None = None
    phone: str | None = None
    email: str | None = None
    country_of_residence: str | None = None
    status: int
    verified: bool
    created_at: datetime
    updated_at: datetime


# Motivos por los que un médico NO puede atender (los produce `services.doctors._blocked_reason`).
# Alias con nombre para que el filtro del listado y la respuesta no puedan divergir.
DoctorBlockedReason = Literal[
    "sin_ficha", "de_baja", "sin_cedula", "sin_licencia", "no_verificado"
]


class DoctorAdminItem(BaseModel):
    """Fila de la tabla de médicos del panel admin.

    El universo son las fichas de `doctors` **más** las cuentas con rol de médico que
    todavía no tienen ficha (`id: null`, `blocked_reason: "sin_ficha"`): esas también hay
    que perseguirlas, y no aparecerían en un listado de `doctors` a secas.

    `can_practice` es el criterio real de acceso (`has_valid_credential`), NO `verified`:
    una ficha verificada sin cédula no atiende. `blocked_reason` dice por qué, y con eso el
    admin sabe qué hacer — solo `no_verificado` se arregla con el botón de aprobar; los
    demás exigen que el médico complete su ficha (o reactivarla).
    """

    id: uuid.UUID | None = None  # null = la cuenta aún no tiene ficha en `doctors`
    user_id: uuid.UUID | None = None
    full_name: str
    cedula: str | None = None
    license: str | None = None
    email: str | None = None
    specialty_id: uuid.UUID | None = None
    professional_type_id: uuid.UUID | None = None
    status: int | None = None  # null = sin ficha
    verified: bool
    created_at: datetime
    can_practice: bool
    blocked_reason: DoctorBlockedReason | None = None


class DoctorAdminPage(BaseModel):
    """Página del listado admin: filas + total exacto (paginación server-side)."""

    items: list[DoctorAdminItem]
    total: int


class DoctorCredentialSummary(BaseModel):
    """Cuántos médicos hay en cada estado de credencial.

    El panel lo pinta como fila de contadores para que el admin vea de entrada qué puede
    hacer: `no_verificado` es su cola de trabajo (aprobar), y `sin_cedula`/`sin_licencia`
    la campaña de recaptura de datos, que no se resuelve desde el panel.

    Los campos coinciden con los valores de `blocked_reason`, así que cada contador es un
    atajo directo al filtro del listado.
    """

    can_practice: int
    sin_ficha: int
    de_baja: int
    sin_cedula: int
    sin_licencia: int
    no_verificado: int
    total: int


class DoctorPoolItem(BaseModel):
    """Fila del pool de médicos: datos mínimos para listar/referir. Sin teléfono: el WhatsApp
    se revela aparte (y se audita) con POST /doctors/{id}/contact.

    El estado "online" NO viene del backend: lo resuelve el frontend con Supabase Realtime
    Presence, cruzando por `user_id`. Los ids de especialidad/tipo los mapea el frontend a
    nombre con sus catálogos ya cargados.
    """

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    full_name: str
    specialty_id: uuid.UUID | None = None
    professional_type_id: uuid.UUID | None = None


class DoctorPoolPage(BaseModel):
    """Página del pool: filas + total (para la paginación server-side del frontend)."""

    items: list[DoctorPoolItem]
    total: int


class DoctorContactResponse(BaseModel):
    """Teléfono de contacto de un médico del pool, revelado bajo auditoría (POST .../contact)."""

    phone: str | None = None
