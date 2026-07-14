"""Business logic for professional_types."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError
from src.models.professional_type import ProfessionalType
from src.schemas.professional_type import ProfessionalTypeCreate, ProfessionalTypeUpdate
from src.services import audit

_RESOURCE = "professional_types"


async def list_professional_types(
    session: AsyncSession, skip: int = 0, limit: int = 100, status: str | None = None
) -> list[ProfessionalType]:
    """Lista tipos no borrados; con `status` filtra además por él (el público usa
    'active' para que desactivar un tipo lo oculte del registro; el admin ve todos)."""
    stmt = select(ProfessionalType).where(ProfessionalType.status != "deleted")
    if status:
        stmt = stmt.where(ProfessionalType.status == status)
    stmt = (
        stmt.order_by(ProfessionalType.created_at.desc(), ProfessionalType.id.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_professional_type(
    session: AsyncSession, professional_type_id: uuid.UUID
) -> ProfessionalType:
    stmt = select(ProfessionalType).where(
        ProfessionalType.id == professional_type_id,
        ProfessionalType.status != "deleted",
    )
    result = await session.execute(stmt)
    professional_type = result.scalar_one_or_none()
    if professional_type is None:
        raise NotFoundError("Professional type not found.")
    return professional_type


async def create_professional_type(
    session: AsyncSession, data: ProfessionalTypeCreate, actor_user_id: uuid.UUID | None = None
) -> ProfessionalType:
    professional_type = ProfessionalType(**data.model_dump())
    session.add(professional_type)
    await session.flush()
    await audit.log_action(
        session,
        action="catalog.created",
        actor_user_id=actor_user_id,
        resource=_RESOURCE,
        resource_id=professional_type.id,
    )
    await session.commit()
    await session.refresh(professional_type)
    return professional_type


async def update_professional_type(
    session: AsyncSession,
    professional_type_id: uuid.UUID,
    data: ProfessionalTypeUpdate,
    actor_user_id: uuid.UUID | None = None,
) -> ProfessionalType:
    professional_type = await get_professional_type(session, professional_type_id)
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(professional_type, field, value)
    await audit.log_action(
        session,
        action="catalog.updated",
        actor_user_id=actor_user_id,
        resource=_RESOURCE,
        resource_id=professional_type.id,
        metadata={"fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(professional_type)
    return professional_type


async def delete_professional_type(
    session: AsyncSession, professional_type_id: uuid.UUID, actor_user_id: uuid.UUID | None = None
) -> None:
    professional_type = await get_professional_type(session, professional_type_id)
    professional_type.status = "deleted"
    professional_type.deleted_at = datetime.now(UTC)
    await audit.log_action(
        session,
        action="catalog.deleted",
        actor_user_id=actor_user_id,
        resource=_RESOURCE,
        resource_id=professional_type.id,
    )
    await session.commit()
