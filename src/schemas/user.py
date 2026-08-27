"""Esquemas Pydantic para la creación administrativa de usuarios (POST /users)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    """Payload de creación: crea el usuario de Auth y, opcionalmente, le asigna un rol."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # bcrypt trunca a 72 bytes: Supabase Auth usa bcrypt internamente.
    password: str = Field(..., min_length=8, max_length=72)
    full_name: str = Field(..., min_length=2, max_length=200)
    # 'super_admin' se acepta aquí solo para producir el 422 explícito del servicio
    # (ver create_user): otorgarlo de verdad solo es posible vía POST /users/{id}/roles.
    initial_role: str | None = Field(default=None, pattern="^(patient|doctor|admin|super_admin)$")


class UserResponse(BaseModel):
    """Respuesta dedicada (allow-list): nunca serializa password, service-role key ni
    el payload crudo de la Admin API de Supabase, aunque `ProfileResponse` cambie."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # `str` y no `EmailStr`: ver la nota en ProfileResponse.email — validar el formato en la
    # SALIDA convierte un dato histórico malo en un 500 de todo el endpoint. `UserCreate.email`
    # sigue siendo `EmailStr` (que es donde importa).
    email: str | None = None
    full_name: str
    role: str
    active: bool
    verified: bool
    created_at: datetime
