"""Esquemas Pydantic para profiles (Response)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ProfileBase(BaseModel):
    email: EmailStr | None = None
    full_name: str
    role: str = "doctor"
    specialty: str | None = None
    medical_license: str | None = None
    country: str | None = None
    whatsapp_number: str | None = None
    verified: bool = False
    active: bool = True
    role_chosen: bool = False
    did_article_8: bool = False
    article_8_doc_path: str | None = None


class ProfileResponse(ProfileBase):
    model_config = ConfigDict(from_attributes=True)

    # `str` y no `EmailStr` en la salida: FastAPI valida la respuesta, así que una sola fila con
    # un email histórico mal formado tumbaría el listado completo con 500 (le pasó a
    # PatientResponse). Hoy `users` está limpia, pero la validación de salida no debe poder
    # convertir un dato viejo en una caída. La entrada la sigue validando ProfileFinalizeRole/
    # UserCreate.
    email: str | None = None

    id: uuid.UUID
    specialty_id: uuid.UUID | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    # Contexto de médico resuelto en el mismo /auth/me (evita una segunda llamada a /doctors/me
    # desde el panel): `has_doctor_profile` = el usuario es médico (con o sin ficha; un admin puro
    # da False); `doctor_cedula` = su cédula (null si aún no la completó). El panel decide con esto
    # si redirige a completar el perfil, sin un segundo round-trip.
    has_doctor_profile: bool = False
    doctor_cedula: str | None = None
    # Roles RBAC efectivos (user_roles; con fallback al legado si la cuenta no tiene filas).
    # En /auth/me, `role` se sobreescribe con el EFECTIVO más alto de esta lista — la columna
    # users.role es un único valor legado y puede quedarse corta (p. ej. dual doctor+super_admin).
    # Solo /auth/me la puebla; en listados (GET /profiles) queda [].
    roles: list[str] = []


class ProfileListResponse(BaseModel):
    """Listado paginado de perfiles + total exacto (para la tabla de médicos/usuarios admin)."""

    items: list[ProfileResponse]
    total: int


class ProfileActiveRequest(BaseModel):
    """Revocar (`active=false`) o reactivar (`active=true`) un médico."""

    model_config = ConfigDict(extra="forbid")

    active: bool


class ProfileFinalizeRoleRequest(BaseModel):
    """Finalizar el rol del propio usuario (solo `patient`/`doctor`)."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(..., pattern="^(patient|doctor)$")
    # El id del catálogo, no el nombre: el nombre que guarda `users.specialty` lo resuelve el
    # backend desde esta FK. Que el cliente eligiera la cadena era como entraban al sistema
    # nombres de especialidad que el catálogo ya no tenía.
    specialty_id: uuid.UUID | None = None
    country: str | None = None
    medical_license: str | None = None
    whatsapp_number: str | None = None
