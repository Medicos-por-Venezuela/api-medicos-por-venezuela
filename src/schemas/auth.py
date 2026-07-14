"""Esquemas Pydantic para la sesión autenticada (roles/permisos RBAC)."""

from pydantic import BaseModel


class PrincipalPermissionsResponse(BaseModel):
    """Roles y permisos RBAC efectivos del usuario autenticado."""

    roles: list[str]
    permissions: list[str]
