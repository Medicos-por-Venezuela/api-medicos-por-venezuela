"""Esquemas Pydantic para doctors (Create / Update / Response).

Los patrones de `cedula` y `phone` reflejan los CHECK de la tabla; la cédula se
normaliza a mayúscula (V-/E-) para casar con el índice único y el CHECK.
"""

import uuid
from datetime import datetime

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
    """Edición (admin). Permite mover `status` (0/1/2) y forzar `verified`."""

    model_config = ConfigDict(extra="forbid")

    specialty_id: uuid.UUID | None = None
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    license: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, pattern=_PHONE_PATTERN)
    email: EmailStr | None = None
    country_of_residence: str | None = Field(default=None, max_length=100)
    status: int | None = Field(default=None, ge=0, le=2)
    verified: bool | None = None


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


class DoctorPoolItem(BaseModel):
    """Fila del pool de médicos: datos mínimos para listar/referir + estado online.

    `online` se deriva en el servicio de `users.last_seen_at` (ventana de 3 min); los
    ids de especialidad/tipo los mapea el frontend a nombre con sus catálogos ya cargados.
    """

    id: uuid.UUID
    full_name: str
    specialty_id: uuid.UUID | None = None
    professional_type_id: uuid.UUID | None = None
    phone: str | None = None
    online: bool


class DoctorPoolPage(BaseModel):
    """Página del pool: filas + total (para la paginación server-side del frontend)."""

    items: list[DoctorPoolItem]
    total: int
