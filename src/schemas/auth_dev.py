"""Esquemas del registro de DEV (solo local; sustituye el signup de Supabase Auth)."""

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class DevRegisterRequest(BaseModel):
    """Registro de prueba en local. `role` solo patient/doctor (nunca admin)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    role: str = Field(default="doctor")
    specialty: str | None = Field(default=None, max_length=120)
    whatsapp_number: str | None = Field(default=None, max_length=40)
    country: str | None = Field(default=None, max_length=100)
    medical_license: str | None = Field(default=None, max_length=100)


class DevAuthResponse(BaseModel):
    """JWT de sesión (firmado con el secret local) + datos básicos del usuario."""

    access_token: str
    token_type: str = "bearer"
    user_id: uuid.UUID
    role: str
    created: bool  # True si se creó; False si ya existía (se devuelve su token)
