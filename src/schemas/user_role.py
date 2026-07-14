"""Esquemas Pydantic para la gestión de roles de usuarios (RBAC)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RoleAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_code: str = Field(..., min_length=2, max_length=50)


class RoleResponse(BaseModel):
    """Un rol del catálogo."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None = None


class UserRoleResponse(BaseModel):
    """Un rol activo asignado a un usuario."""

    id: uuid.UUID  # id de la asignación (user_roles.id)
    role_id: uuid.UUID
    role_code: str
    role_name: str
    assigned_at: datetime
