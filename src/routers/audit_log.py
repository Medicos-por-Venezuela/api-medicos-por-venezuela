"""Capa HTTP (delgada) para leer el registro de auditoría.

Solo lectura (append-only en la BD). Requiere el permiso `audit.read`.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, require_permission
from src.db.session import get_db
from src.schemas.audit_log import AuditLogResponse
from src.services import audit as audit_service

router = APIRouter(prefix="/audit-log", tags=["audit"])
tag_metadata = [
    {"name": "audit", "description": "Registro de auditoría (solo lectura; permiso audit.read)."}
]


@router.get(
    "",
    response_model=list[AuditLogResponse],
    summary="Ver el registro de auditoría",
)
async def list_audit_log(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    action: str | None = Query(None, description="Filtra por acción, p. ej. role.assigned"),
    actor_user_id: uuid.UUID | None = Query(None, description="Filtra por el usuario que actuó"),
    resource: str | None = Query(None, description="Filtra por tipo de recurso"),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_permission("audit.read")),
) -> list[AuditLogResponse]:
    """Entradas del audit_log, más recientes primero. Filtros opcionales por
    `action`, `actor_user_id` y `resource`."""
    return await audit_service.list_audit_log(
        db, skip=skip, limit=limit, action=action, actor_user_id=actor_user_id, resource=resource
    )
