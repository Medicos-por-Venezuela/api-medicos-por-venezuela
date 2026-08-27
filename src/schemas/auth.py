"""Esquemas Pydantic para la sesión autenticada (roles/permisos RBAC)."""

from pydantic import BaseModel


class PrincipalPermissionsResponse(BaseModel):
    """Roles y permisos RBAC efectivos del usuario autenticado."""

    roles: list[str]
    permissions: list[str]
    # Médico habilitado para atender (ficha verificada con cédula y licencia). Cuando es
    # False el usuario mantiene su rol `doctor` pero `permissions` viene VACÍO: está
    # registrado y a la espera de que el SACS/FPV valide su cédula o de que un admin lo
    # apruebe. El frontend usa este flag para mostrar "pendiente de verificación" en vez
    # de una pantalla sin permisos. Para pacientes y admins es siempre True.
    credential_verified: bool = True
