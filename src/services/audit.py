"""Escritura del registro de auditoría (append-only).

`log_action` agrega una entrada al `audit_log` en la sesión actual. **No** commitea:
la entrada se persiste junto con la transacción del caller, así el audit es atómico
con la acción auditada (si la acción falla y hace rollback, no queda audit huérfano).
El `audit_log` es inmutable a nivel de BD (un trigger rechaza UPDATE/DELETE).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit_log import AuditLog


async def log_action(
    session: AsyncSession,
    *,
    action: str,
    actor_user_id: uuid.UUID | None = None,
    resource: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    metadata: dict | None = None,
    ip: str | None = None,
    correlation_id: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        resource=resource,
        resource_id=str(resource_id) if resource_id is not None else None,
        metadata_=metadata,
        ip=ip,
        correlation_id=correlation_id,
    )
    session.add(entry)
    return entry
