"""El CONJUNTO de especialidades que ejerce un médico (`doctor_specialties`).

Un médico puede tener varias (un internista que además es cardiólogo) y su cola es la unión de
todas — lo decide `services/queue_access.py`. `users.specialty_id` sigue siendo la **principal**:
la que usan el pool, los reportes, el admin y la bandeja de interconsultas.

Estas funciones NO hacen commit: se persisten en la transacción del caller, como el resto de la
capa de servicios.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import UnprocessableError
from src.models.doctor_specialty import DoctorSpecialty
from src.models.specialty import Specialty

# Tope defensivo: nadie ejerce veinte especialidades, y un payload absurdo no llena la tabla.
MAX_SPECIALTIES = 10


async def list_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[Specialty]:
    """Las especialidades de la cuenta, por el orden del catálogo."""
    return list(
        (
            await session.scalars(
                select(Specialty)
                .join(DoctorSpecialty, DoctorSpecialty.specialty_id == Specialty.id)
                .where(DoctorSpecialty.user_id == user_id, Specialty.deleted_at.is_(None))
                .order_by(Specialty.sort_order, Specialty.name, Specialty.id)
            )
        ).all()
    )


async def validate(session: AsyncSession, specialty_ids: list[uuid.UUID]) -> list[Specialty]:
    """Comprueba que sean especialidades reales del catálogo y devuelve las filas, sin repetir.

    Las de relleno ("Otra") se descartan: no son la cola de nadie. Elegirlas es lo mismo que decir
    "mi especialidad no está", que va por `requested_specialty`."""
    if len(specialty_ids) > MAX_SPECIALTIES:
        raise UnprocessableError(f"Puedes elegir hasta {MAX_SPECIALTIES} especialidades.")
    unicas: list[uuid.UUID] = []
    for specialty_id in specialty_ids:
        if specialty_id not in unicas:
            unicas.append(specialty_id)
    filas: list[Specialty] = []
    for specialty_id in unicas:
        specialty = await session.get(Specialty, specialty_id)
        if specialty is None or specialty.deleted_at is not None or specialty.status != "active":
            raise UnprocessableError("Alguna de las especialidades elegidas no es válida.")
        if not specialty.is_placeholder:
            filas.append(specialty)
    return filas


async def replace(
    session: AsyncSession, user_id: uuid.UUID, specialty_ids: list[uuid.UUID]
) -> None:
    """Deja el conjunto EXACTAMENTE en `specialty_ids` (sin commit)."""
    await session.execute(delete(DoctorSpecialty).where(DoctorSpecialty.user_id == user_id))
    for specialty_id in dict.fromkeys(specialty_ids):
        session.add(DoctorSpecialty(user_id=user_id, specialty_id=specialty_id))
    await session.flush()


async def add(session: AsyncSession, user_id: uuid.UUID, specialty_id: uuid.UUID) -> None:
    """Suma una especialidad al conjunto si no estaba (sin commit)."""
    existe = await session.get(DoctorSpecialty, {"user_id": user_id, "specialty_id": specialty_id})
    if existe is None:
        session.add(DoctorSpecialty(user_id=user_id, specialty_id=specialty_id))
        await session.flush()
