"""Specialty catalog, matching rules, and CRUD."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError
from src.models.specialty import Specialty
from src.schemas.specialty import SpecialtyCreate, SpecialtyUpdate
from src.services import audit

_RESOURCE = "specialties"

# Necesidades que elevan la prioridad a "review" (registro-paciente).
_PRIORITY_REVIEW_TAGS = {"Lesión física", "Embarazo", "Niño / pediatría"}


@dataclass(frozen=True)
class SpecialtyFlags:
    """Reserva de salud mental de una especialidad, tal y como está en el catálogo."""

    is_mental_health: bool = False
    mental_health_only: bool = False


async def flags_for_specialty_id(
    session: AsyncSession, specialty_id: uuid.UUID | None
) -> SpecialtyFlags:
    """Flags de la especialidad de un MÉDICO, resueltos por su FK (`users.specialty_id`).

    Por id y no por nombre: renombrar una especialidad en el catálogo no puede cambiar quién
    puede atender qué. Si el médico no tiene especialidad devuelve todo en False, que es
    fail-closed en la dirección que importa: sin `is_mental_health` NO puede tomar un caso de
    salud mental.
    """
    if specialty_id is None:
        return SpecialtyFlags()
    row = (
        await session.execute(
            select(Specialty.is_mental_health, Specialty.mental_health_only).where(
                Specialty.id == specialty_id
            )
        )
    ).first()
    return SpecialtyFlags(*row) if row else SpecialtyFlags()


async def name_for_id(session: AsyncSession, specialty_id: uuid.UUID | None) -> str | None:
    """Nombre del catálogo para una especialidad. Lo usan los escritores de `users.specialty`,
    que es una copia desnormalizada: el nombre SIEMPRE sale de la fila, nunca del cliente."""
    if specialty_id is None:
        return None
    return await session.scalar(select(Specialty.name).where(Specialty.id == specialty_id))


def can_attend_consultation(
    *,
    doctor: SpecialtyFlags,
    consultation_is_mental_health: bool,
) -> bool:
    """Elegibilidad dura: separación bidireccional entre salud mental y salud física.

    1) Un caso de salud mental solo lo toma quien atiende salud mental.
    2) Quien SOLO atiende salud mental (Psicología, que no es médico) no toma casos físicos.

    Que la especialidad del caso coincida exactamente con la del médico es la PREFERENCIA de
    "atender al siguiente", no un bloqueo: nadie se queda sin atender.

    Ambas reglas salen de columnas de `specialties`, no de nombres. Antes eran los literales
    `_PSYCH_SPECIALTIES` y `!= "Psicología"`, que un renombre del catálogo rompía en silencio —
    y en la dirección peligrosa: un caso de salud mental habría pasado a poder tomarlo cualquiera.
    """
    if consultation_is_mental_health:
        return doctor.is_mental_health
    return not doctor.mental_health_only


def compute_priority(needs_tags: list[str] | None) -> str:
    """'review' si hay una necesidad sensible; 'normal' en caso contrario."""
    if needs_tags and _PRIORITY_REVIEW_TAGS.intersection(needs_tags):
        return "review"
    return "normal"


async def _ensure_unique_specialty_name(
    session: AsyncSession, name: str, specialty_id: uuid.UUID | None = None
) -> None:
    stmt = select(Specialty.id).where(
        func.lower(Specialty.name) == name.lower(), Specialty.deleted_at.is_(None)
    )
    if specialty_id is not None:
        stmt = stmt.where(Specialty.id != specialty_id)
    if (await session.execute(stmt)).first():
        raise ConflictError("Ya existe una especialidad con ese nombre.")


async def list_specialties(
    session: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    for_interconsultation: bool | None = None,
) -> list[Specialty]:
    """Catálogo de especialidades. `for_interconsultation=True` deja solo las que se pueden
    pedir en una interconsulta asíncrona (excluye Medicina general): es el selector del médico
    tratante. El filtro sale de la columna, nunca de comparar nombres."""
    stmt = select(Specialty).where(Specialty.deleted_at.is_(None))
    if status:
        stmt = stmt.where(Specialty.status == status)
    if for_interconsultation is not None:
        stmt = stmt.where(Specialty.available_for_interconsultation.is_(for_interconsultation))
    stmt = (
        stmt.order_by(Specialty.sort_order.asc(), Specialty.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_specialty(session: AsyncSession, specialty_id: uuid.UUID) -> Specialty:
    specialty = await session.get(Specialty, specialty_id)
    if specialty is None or specialty.deleted_at is not None:
        raise NotFoundError("Especialidad no encontrada.")
    return specialty


async def create_specialty(
    session: AsyncSession, data: SpecialtyCreate, actor_user_id: uuid.UUID | None = None
) -> Specialty:
    await _ensure_unique_specialty_name(session, data.name)
    specialty = Specialty(**data.model_dump())
    session.add(specialty)
    await session.flush()
    await audit.log_action(
        session,
        action="catalog.created",
        actor_user_id=actor_user_id,
        resource=_RESOURCE,
        resource_id=specialty.id,
    )
    await session.commit()
    await session.refresh(specialty)
    return specialty


async def update_specialty(
    session: AsyncSession,
    specialty_id: uuid.UUID,
    data: SpecialtyUpdate,
    actor_user_id: uuid.UUID | None = None,
) -> Specialty:
    specialty = await get_specialty(session, specialty_id)
    changes = data.model_dump(exclude_unset=True)
    if "name" in changes:
        await _ensure_unique_specialty_name(session, changes["name"], specialty_id)
    for field, value in changes.items():
        setattr(specialty, field, value)
    await audit.log_action(
        session,
        action="catalog.updated",
        actor_user_id=actor_user_id,
        resource=_RESOURCE,
        resource_id=specialty.id,
        metadata={"fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(specialty)
    return specialty


async def delete_specialty(
    session: AsyncSession, specialty_id: uuid.UUID, actor_user_id: uuid.UUID | None = None
) -> None:
    specialty = await get_specialty(session, specialty_id)
    specialty.status = "inactive"
    specialty.deleted_at = datetime.now(UTC)
    await audit.log_action(
        session,
        action="catalog.deleted",
        actor_user_id=actor_user_id,
        resource=_RESOURCE,
        resource_id=specialty.id,
    )
    await session.commit()
