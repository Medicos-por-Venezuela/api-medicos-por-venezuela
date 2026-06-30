"""Business logic for professional_types."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError
from src.models.professional_type import ProfessionalType
from src.schemas.professional_type import ProfessionalTypeCreate, ProfessionalTypeUpdate


async def list_professional_types(
    session: AsyncSession, skip: int = 0, limit: int = 100
) -> list[ProfessionalType]:
    stmt = (
        select(ProfessionalType)
        .where(ProfessionalType.status != "deleted")
        .order_by(ProfessionalType.created_at.desc(), ProfessionalType.id.desc())
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
    session: AsyncSession, data: ProfessionalTypeCreate
) -> ProfessionalType:
    professional_type = ProfessionalType(**data.model_dump())
    session.add(professional_type)
    await session.commit()
    await session.refresh(professional_type)
    return professional_type


async def update_professional_type(
    session: AsyncSession, professional_type_id: uuid.UUID, data: ProfessionalTypeUpdate
) -> ProfessionalType:
    professional_type = await get_professional_type(session, professional_type_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(professional_type, field, value)
    if professional_type.status == "deleted":
        professional_type.deleted_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(professional_type)
    return professional_type


async def delete_professional_type(session: AsyncSession, professional_type_id: uuid.UUID) -> None:
    professional_type = await get_professional_type(session, professional_type_id)
    professional_type.status = "deleted"
    professional_type.deleted_at = datetime.now(UTC)
    await session.commit()
