"""Specialty catalog, matching rules, and CRUD."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ConflictError, NotFoundError
from src.models.specialty import Specialty
from src.schemas.specialty import SpecialtyCreate, SpecialtyUpdate
from src.services import audit

_RESOURCE = "specialties"

# Catálogo de especialidades de los médicos (lib/utils.ts: SPECIALTIES).
SPECIALTIES: list[str] = [
    "Medicina general",
    "Pediatría",
    "Traumatología",
    "Ginecología",
    "Obstetricia",
    "Cardiología",
    "Medicina interna",
    "Psicología",
    "Psiquiatría",
    "Neurología",
    "Cirugía",
    "Oncología",
    "Oncología médica",
    "Fisiatría",
    "Cuidados paliativos y manejo del dolor",
    "Geriatría",
    "Reumatología",
    "Otra",
]

# Catálogo de necesidades del paciente (registro-paciente: NECESIDADES).
NEEDS: list[str] = [
    "Medicina general",
    "Lesión física",
    "Primeros auxilios",
    "Apoyo emocional",
    "Crisis de ansiedad",
    "Niño / pediatría",
    "Embarazo",
    "Medicamentos",
    "Enfermedad crónica",
    "Otra",
]

# Especialidad -> necesidades que cubre ('*' = todo). (lib/utils.ts: SPECIALTY_NEEDS).
SPECIALTY_NEEDS: dict[str, list[str]] = {
    "Medicina general": ["*"],
    "Medicina interna": [
        "Medicina general",
        "Enfermedad crónica",
        "Medicamentos",
        "Primeros auxilios",
    ],
    "Pediatría": ["Niño / pediatría"],
    "Traumatología": ["Lesión física"],
    "Ginecología": ["Embarazo"],
    "Obstetricia": ["Embarazo"],
    "Cardiología": ["Enfermedad crónica"],
    "Psicología": ["Apoyo emocional", "Crisis de ansiedad"],
    "Psiquiatría": ["Apoyo emocional", "Crisis de ansiedad"],
    "Neurología": ["Enfermedad crónica"],
    "Cirugía": ["Lesión física"],
    "Oncología": ["Enfermedad crónica"],
    "Oncología médica": ["Enfermedad crónica"],
    "Fisiatría": ["Lesión física"],
    "Cuidados paliativos y manejo del dolor": ["Enfermedad crónica"],
    "Geriatría": ["Enfermedad crónica", "Medicina general"],
    "Reumatología": ["Enfermedad crónica"],
    "Otra": ["*"],
}

# Necesidades reservadas a salud mental (nunca caen en médicos generales).
RESERVED_NEEDS: dict[str, list[str]] = {
    "Apoyo emocional": ["Psicología", "Psiquiatría"],
    "Crisis de ansiedad": ["Psicología", "Psiquiatría"],
}

# Necesidades que elevan la prioridad a "review" (registro-paciente).
_PRIORITY_REVIEW_TAGS = {"Lesión física", "Embarazo", "Niño / pediatría"}


def _values(category: str | None, needs_tags: list[str] | None) -> list[str]:
    return [v for v in [category, *(needs_tags or [])] if v]


def matches_specialty(
    specialty: str | None, category: str | None, needs_tags: list[str] | None
) -> bool:
    """True si la consulta (category + needs_tags) alinea con la especialidad."""
    if not specialty:
        return False
    covered = SPECIALTY_NEEDS.get(specialty)
    if not covered:
        return False
    if "*" in covered:
        return True
    return any(v in covered for v in _values(category, needs_tags))


def can_attend(specialty: str | None, category: str | None, needs_tags: list[str] | None) -> bool:
    """Elegibilidad dura (separación bidireccional psicología <-> salud física)."""
    values = _values(category, needs_tags)

    reserved_ok = all(
        (v not in RESERVED_NEEDS) or (bool(specialty) and specialty in RESERVED_NEEDS[v])
        for v in values
    )
    if not reserved_ok:
        return False

    if specialty == "Psicología":
        is_psych_case = any(v in RESERVED_NEEDS for v in values)
        if not is_psych_case:
            return False
    return True


# El registro del paciente ahora pide la especialidad y la guarda en consultations.specialty_id:
# esa columna ES el matching. category/needs_tags quedan como fallback para consultas viejas.
_PSYCH_SPECIALTIES = {"Psicología", "Psiquiatría"}


def matches_consultation(
    specialty: str | None,
    consultation_specialty: str | None,
    category: str | None,
    needs_tags: list[str] | None,
) -> bool:
    """Match de preferencia: igualdad exacta con la especialidad solicitada por el paciente;
    fallback al matching legacy (category/needs) solo para consultas sin specialty_id."""
    if consultation_specialty:
        return specialty == consultation_specialty
    return matches_specialty(specialty, category, needs_tags)


def can_attend_consultation(
    specialty: str | None,
    consultation_specialty: str | None,
    category: str | None,
    needs_tags: list[str] | None,
) -> bool:
    """Elegibilidad dura con la especialidad explícita. La reserva de psicología se mantiene:
    un caso de salud mental solo va a Psicología/Psiquiatría, y Psicología solo atiende salud
    mental. Un caso físico explícito lo puede tomar cualquier no-psicólogo (que la especialidad
    coincida es la PREFERENCIA de attend-next, no un bloqueo — nadie se queda sin atender)."""
    if consultation_specialty in _PSYCH_SPECIALTIES:
        return specialty in _PSYCH_SPECIALTIES
    if consultation_specialty:
        return specialty != "Psicología"
    return can_attend(specialty, category, needs_tags)


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
    session: AsyncSession, skip: int = 0, limit: int = 100, status: str | None = None
) -> list[Specialty]:
    stmt = select(Specialty).where(Specialty.deleted_at.is_(None))
    if status:
        stmt = stmt.where(Specialty.status == status)
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
