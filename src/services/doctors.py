"""Capa de negocio para doctors."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError
from src.models.doctor import Doctor
from src.schemas.doctor import DoctorCreate, DoctorUpdate


async def list_doctors(
    session: AsyncSession, skip: int = 0, limit: int = 100, status: str | None = None
) -> list[Doctor]:
    stmt = select(Doctor)
    if status:
        stmt = stmt.where(Doctor.status == status)
    stmt = stmt.order_by(Doctor.created_at.desc()).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_doctor(session: AsyncSession, doctor_id: uuid.UUID) -> Doctor:
    doctor = await session.get(Doctor, doctor_id)
    if doctor is None:
        raise NotFoundError("Médico no encontrado.")
    return doctor


async def create_doctor(session: AsyncSession, data: DoctorCreate) -> Doctor:
    doctor = Doctor(**data.model_dump())
    session.add(doctor)
    await session.commit()
    await session.refresh(doctor)
    return doctor


async def update_doctor(session: AsyncSession, doctor_id: uuid.UUID, data: DoctorUpdate) -> Doctor:
    doctor = await get_doctor(session, doctor_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(doctor, field, value)
    await session.commit()
    await session.refresh(doctor)
    return doctor


async def delete_doctor(session: AsyncSession, doctor_id: uuid.UUID) -> None:
    doctor = await get_doctor(session, doctor_id)
    await session.delete(doctor)
    await session.commit()
