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
    """Forma común de un perfil. La comparten los endpoints que devuelven UNO
    (`GET /profiles/{id}`, `PATCH /profiles/{id}/active`, `POST /profiles/me/finalize-role`).

    Deliberadamente NO trae `has_doctor_profile`, `doctor_cedula`, `roles` ni `doctor_verified`:
    cada uno lo puebla un endpoint distinto, y tenerlos aquí hacía que los demás los devolvieran
    con su valor por defecto — es decir, afirmando algo falso. La lista del admin llegó a decir
    `has_doctor_profile: false` y `doctor_verified: true` en la MISMA fila. Cada subclase de abajo
    declara solo lo que su endpoint realmente rellena.
    """

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


class MyProfileResponse(ProfileResponse):
    """`GET /auth/me`. Solo aquí tienen sentido —y solo aquí se pueblan— el contexto de médico y
    los roles RBAC efectivos."""

    # Contexto de médico resuelto en el mismo /auth/me (evita una segunda llamada a /doctors/me
    # desde el panel): `has_doctor_profile` = el usuario es médico (con o sin ficha; un admin puro
    # da False); `doctor_cedula` = su cédula (null si aún no la completó). El panel decide con esto
    # si redirige a completar el perfil, sin un segundo round-trip.
    has_doctor_profile: bool = False
    doctor_cedula: str | None = None
    # Roles RBAC efectivos (user_roles; con fallback al legado si la cuenta no tiene filas).
    # `role` se sobreescribe con el EFECTIVO más alto de esta lista — la columna users.role es un
    # único valor legado y puede quedarse corta (p. ej. dual doctor+super_admin).
    roles: list[str] = []


class ProfileListItem(ProfileResponse):
    """Fila del listado del admin (`GET /profiles`)."""

    # `doctors.verified`: resultado de contrastar la cédula con SACS (médico) o FPV (psicólogo).
    # `None` = esta persona no tiene ficha, así que no hay credencial que verificar.
    #
    # Se llama `doctor_verified` y NO `verified` a propósito: el schema ya tiene un `verified`, el
    # de `users`, que nace `true` y ningún camino la baja. Reutilizar el nombre es exactamente cómo
    # la lista acabó pintando como "Verificado" a los médicos cuya cédula no validó.
    doctor_verified: bool | None = None


class ProfileListResponse(BaseModel):
    """Listado paginado de perfiles + total exacto (para la tabla de médicos/usuarios admin)."""

    items: list[ProfileListItem]
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
