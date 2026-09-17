"""Qué colas ve y puede tomar cada médico (R1 de tasks/cola-por-especialidad/spec.md).

Es la ÚNICA definición de la regla. Listar la cola (`get_panel`, `GET /queue`), tomar un caso
(`claim`, `/queue/{id}/take`) y derivarlo desde la cola salen de aquí: si dos de esos caminos
tuvieran su propia versión, la lista diría una cosa y el POST directo otra — que es justo como
un psicólogo terminó tomando casos de Medicina general.

La regla:
1. Un admin ve todas las colas, salvo que TODAS sus especialidades sean de salud mental exclusiva
   (Psicología): entonces aplica la regla normal. Si además ejerce alguna especialidad (no solo
   Medicina general), el panel le arma sus colas y una última con TODO lo demás, que sigue viendo.
2. Sin especialidad, o solo con la de relleno ("Otra"): no ve ninguna cola hasta actualizar su
   perfil.
3. Resto: **todas sus especialidades** (`doctor_specialties`; un internista que además es
   cardiólogo ve las dos), cada una con los accesos extra de `specialty_queue_access`.
4. Cola de entrada (`specialties.is_general_triage`, Medicina general): la ve además quien atiende
   salud física, porque ahí caen los pacientes que no saben qué especialidad necesitan. Quien solo
   atiende salud mental, no.

El panel pinta una card por grupo (`QueueGroup`) cuando hay más de uno.

Todo sale de columnas y tablas del catálogo, nunca de nombres: renombrar una especialidad no
puede abrir ni cerrar una cola en silencio.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, false, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ForbiddenError
from src.models.consultation import Consultation
from src.models.doctor_specialty import DoctorSpecialty
from src.models.specialty import Specialty
from src.models.specialty_queue_access import SpecialtyQueueAccess

# Por qué un médico no ve ninguna cola. Lo pinta el panel para decirle qué hacer.
SIN_ESPECIALIDAD = "sin_especialidad"
ESPECIALIDAD_POR_DEFINIR = "especialidad_por_definir"


# La cola "todo lo demás" de un admin que además ejerce: no es una especialidad del catálogo.
RESTO = "Otras especialidades"


@dataclass(frozen=True)
class QueueGroup:
    """Una cola del panel: la especialidad que la titula y los ids de casos que entran en ella
    (la suya más sus accesos extra, p. ej. Psicología dentro de la de Psiquiatría).

    `specialty` es None en la cola del resto (`is_rest`): la de un admin que ejerce, donde cae
    todo lo que no es de sus especialidades. El panel la calcula por descarte, así que no lleva
    ids: enumerar el catálogo entero aquí solo daría una lista que caduca al crear una
    especialidad."""

    specialty: Specialty | None
    specialty_ids: frozenset[uuid.UUID]
    is_triage: bool = False
    is_rest: bool = False

    @property
    def id(self) -> uuid.UUID | None:
        return self.specialty.id if self.specialty is not None else None

    @property
    def name(self) -> str:
        return self.specialty.name if self.specialty is not None else RESTO


@dataclass(frozen=True)
class QueueScope:
    """Las colas que alguien puede ver y tomar.

    `specialty_ids` es None cuando ve todas; un conjunto (quizá vacío) cuando solo ve esas.
    `blocked_reason` explica un conjunto vacío. `groups` son las colas que el panel pinta por
    separado; va vacío cuando no hay nada que separar (un médico general, o un admin que no
    ejerce ninguna especialidad: todo lo que ve es una sola lista)."""

    specialty_ids: frozenset[uuid.UUID] | None
    blocked_reason: str | None = None
    groups: list[QueueGroup] = field(default_factory=list)

    def allows(self, specialty_id: uuid.UUID | None) -> bool:
        if self.specialty_ids is None:
            return True
        return specialty_id is not None and specialty_id in self.specialty_ids

    def sql_filter(self) -> ColumnElement[bool]:
        """Condición SQL equivalente a `allows` sobre `consultations.specialty_id`."""
        if self.specialty_ids is None:
            return true()
        if not self.specialty_ids:
            return false()
        return Consultation.specialty_id.in_(self.specialty_ids)


async def general_triage_specialty(session: AsyncSession) -> Specialty | None:
    """La cola de entrada del catálogo (Medicina general), por su columna y no por su nombre."""
    return await session.scalar(
        select(Specialty)
        .where(
            Specialty.is_general_triage.is_(True),
            Specialty.deleted_at.is_(None),
            Specialty.status == "active",
        )
        .order_by(Specialty.sort_order, Specialty.id)
        .limit(1)
    )


async def doctor_specialties(
    session: AsyncSession, user_id: uuid.UUID | None, primary_id: uuid.UUID | None
) -> list[Specialty]:
    """Las especialidades que ejerce esta cuenta, la principal primero.

    `doctor_specialties` es el conjunto; `users.specialty_id` (la principal) es el respaldo para
    las cuentas que todavía no tienen filas ahí —el backfill de la migración las cubre, pero una
    cuenta creada por otro camino no puede quedarse sin cola por eso."""
    rows: list[Specialty] = []
    if user_id is not None:
        rows = list(
            (
                await session.scalars(
                    select(Specialty)
                    .join(DoctorSpecialty, DoctorSpecialty.specialty_id == Specialty.id)
                    .where(DoctorSpecialty.user_id == user_id, Specialty.deleted_at.is_(None))
                    .order_by(Specialty.sort_order, Specialty.name, Specialty.id)
                )
            ).all()
        )
    if not rows and primary_id is not None:
        primary = await session.get(Specialty, primary_id)
        rows = [primary] if primary is not None else []
    # La principal primero: es la que titula al médico en el resto del sistema.
    rows.sort(key=lambda s: s.id != primary_id)
    return rows


async def _extras(session: AsyncSession, specialty_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        (
            await session.scalars(
                select(SpecialtyQueueAccess.extra_specialty_id).where(
                    SpecialtyQueueAccess.specialty_id == specialty_id
                )
            )
        ).all()
    )


async def queue_scope(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    specialty_id: uuid.UUID | None,
    is_admin: bool,
) -> QueueScope:
    """Colas visibles para una cuenta: sus especialidades (`doctor_specialties`), sus accesos
    extra y la de entrada si atiende salud física."""
    mias = await doctor_specialties(session, user_id, specialty_id)
    reales = [s for s in mias if not s.is_placeholder]
    triage = await general_triage_specialty(session)

    if is_admin and not (reales and all(s.mental_health_only for s in reales)):
        # Ve TODAS las colas. Solo se le separan en cards si además es especialista: sus colas y
        # una última con el resto, que sigue viendo. A un admin que no ejerce, o que solo ejerce
        # Medicina general, partirle el panel le dejaba una card "mi especialidad" que en realidad
        # traía todas las demás.
        grupos: list[QueueGroup] = []
        if any(not es_triage(s, triage) for s in reales):
            grupos = await _grupos(session, reales, triage)
            grupos.append(QueueGroup(None, frozenset(), is_rest=True))
        return QueueScope(None, groups=grupos)
    if not mias:
        return QueueScope(frozenset(), SIN_ESPECIALIDAD)
    if not reales:
        return QueueScope(frozenset(), ESPECIALIDAD_POR_DEFINIR)

    grupos = await _grupos(session, reales, triage)
    vistos = {sid for g in grupos for sid in g.specialty_ids}
    return QueueScope(frozenset(vistos), groups=grupos)


def es_triage(specialty: Specialty, triage: Specialty | None) -> bool:
    return triage is not None and specialty.id == triage.id


async def _grupos(
    session: AsyncSession, reales: list[Specialty], triage: Specialty | None
) -> list[QueueGroup]:
    """Una cola por especialidad del médico (con sus accesos extra) más la de entrada."""
    grupos: list[QueueGroup] = []
    vistos: set[uuid.UUID] = set()
    tiene_triage = any(es_triage(s, triage) for s in reales)
    for especialidad in reales:
        ids = {especialidad.id, *await _extras(session, especialidad.id)}
        if triage is not None and not tiene_triage:
            ids.discard(triage.id)  # la cola de entrada va en su propia card
        nuevos = ids - vistos
        if not nuevos:
            continue
        vistos |= nuevos
        grupos.append(
            QueueGroup(
                specialty=especialidad,
                specialty_ids=frozenset(nuevos),
                is_triage=es_triage(especialidad, triage),
            )
        )

    # La cola de entrada la atiende también el especialista de salud física: es la que acumula
    # (el paciente que no sabe qué necesita cae ahí). Quien SOLO atiende salud mental, no. Va
    # primera en el panel: es la que más pacientes tiene esperando.
    if triage is not None and not tiene_triage and any(not s.mental_health_only for s in reales):
        grupos.insert(
            0, QueueGroup(specialty=triage, specialty_ids=frozenset({triage.id}), is_triage=True)
        )

    return grupos


async def ensure_can_take(
    session: AsyncSession,
    consultation: Consultation,
    *,
    user_id: uuid.UUID | None = None,
    specialty_id: uuid.UUID | None,
    is_admin: bool,
) -> None:
    """403 si el caso no está en ninguna de las colas de quien lo pide."""
    scope = await queue_scope(
        session, user_id=user_id, specialty_id=specialty_id, is_admin=is_admin
    )
    if not scope.allows(consultation.specialty_id):
        raise ForbiddenError("Este caso no corresponde a tu especialidad.")
