"""Business logic for affected_zones."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError
from src.models.affected_zone import AffectedZone
from src.schemas.affected_zone import AffectedZoneCreate, AffectedZoneUpdate


async def _ensure_unique_name_state(
    session: AsyncSession,
    name: str,
    state: str,
    zone_id: uuid.UUID | None = None,
) -> None:
    stmt = select(AffectedZone.id).where(
        func.lower(AffectedZone.name) == name.lower(),
        func.lower(AffectedZone.state) == state.lower(),
        AffectedZone.deleted_at.is_(None),
    )
    if zone_id is not None:
        stmt = stmt.where(AffectedZone.id != zone_id)
    if (await session.execute(stmt)).first():
        raise ConflictError("Ya existe una zona afectada con ese nombre en ese estado.")


async def list_affected_zones(
    session: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
) -> list[AffectedZone]:
    stmt = select(AffectedZone).where(AffectedZone.deleted_at.is_(None))
    if status:
        stmt = stmt.where(AffectedZone.status == status)
    stmt = stmt.order_by(AffectedZone.name.asc()).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_affected_zone(session: AsyncSession, zone_id: uuid.UUID) -> AffectedZone:
    stmt = select(AffectedZone).where(
        AffectedZone.id == zone_id,
        AffectedZone.deleted_at.is_(None),
    )
    result = await session.execute(stmt)
    zone = result.scalar_one_or_none()
    if zone is None:
        raise NotFoundError("Zona afectada no encontrada.")
    return zone


async def get_active_affected_zone(session: AsyncSession, zone_id: uuid.UUID) -> AffectedZone:
    zone = await get_affected_zone(session, zone_id)
    if zone.status != "active":
        raise NotFoundError("Zona afectada no encontrada.")
    return zone


async def create_affected_zone(session: AsyncSession, data: AffectedZoneCreate) -> AffectedZone:
    await _ensure_unique_name_state(session, data.name, data.state)
    zone = AffectedZone(**data.model_dump())
    session.add(zone)
    await session.commit()
    await session.refresh(zone)
    return zone


async def update_affected_zone(
    session: AsyncSession, zone_id: uuid.UUID, data: AffectedZoneUpdate
) -> AffectedZone:
    zone = await get_affected_zone(session, zone_id)
    changes = data.model_dump(exclude_unset=True)
    if "name" in changes or "state" in changes:
        new_name = changes.get("name", zone.name)
        new_state = changes.get("state", zone.state)
        await _ensure_unique_name_state(session, new_name, new_state, zone_id)
    for field, value in changes.items():
        setattr(zone, field, value)
    await session.commit()
    await session.refresh(zone)
    return zone


async def delete_affected_zone(session: AsyncSession, zone_id: uuid.UUID) -> None:
    zone = await get_affected_zone(session, zone_id)
    zone.status = "deleted"
    zone.deleted_at = datetime.now(UTC)
    await session.commit()
