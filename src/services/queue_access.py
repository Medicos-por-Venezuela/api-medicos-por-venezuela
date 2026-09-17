"""Quién ve y quién puede tomar cada caso de la cola (R1 de tasks/cola-por-especialidad/spec.md).

Es la ÚNICA definición de la regla. Listar la cola (`get_panel`, `GET /queue`), tomar un caso
(`claim`, `/queue/{id}/take`) y derivarlo desde la cola salen de aquí: si dos de esos caminos
tuvieran su propia versión, la lista diría una cosa y el POST directo otra — que es justo como
un psicólogo terminó tomando casos de Medicina general.

La regla:
1. Un admin ve todas las colas, salvo que su especialidad sea SOLO de salud mental
   (Psicología): entonces aplica la regla normal.
2. Sin especialidad, o con una de relleno ("Otra"): no ve ninguna cola hasta actualizar su perfil.
3. Resto: la cola de su especialidad exacta más las de `specialty_queue_access`.

Todo sale de columnas y tablas del catálogo, nunca de nombres: renombrar una especialidad no
puede abrir ni cerrar una cola en silencio.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, false, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import ForbiddenError
from src.models.consultation import Consultation
from src.models.specialty import Specialty
from src.models.specialty_queue_access import SpecialtyQueueAccess

# Por qué un médico no ve ninguna cola. Lo pinta el panel para decirle qué hacer.
SIN_ESPECIALIDAD = "sin_especialidad"
ESPECIALIDAD_POR_DEFINIR = "especialidad_por_definir"


@dataclass(frozen=True)
class QueueScope:
    """Las colas que alguien puede ver y tomar.

    `specialty_ids` es None cuando ve todas; un conjunto (quizá vacío) cuando solo ve esas.
    `blocked_reason` explica un conjunto vacío."""

    specialty_ids: frozenset[uuid.UUID] | None
    blocked_reason: str | None = None

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


async def queue_scope(
    session: AsyncSession, *, specialty_id: uuid.UUID | None, is_admin: bool
) -> QueueScope:
    """Colas visibles para un médico con `specialty_id` (`users.specialty_id`)."""
    specialty = await session.get(Specialty, specialty_id) if specialty_id is not None else None
    if is_admin and not (specialty is not None and specialty.mental_health_only):
        return QueueScope(None)
    if specialty is None:
        return QueueScope(frozenset(), SIN_ESPECIALIDAD)
    if specialty.is_placeholder:
        return QueueScope(frozenset(), ESPECIALIDAD_POR_DEFINIR)
    extra = await session.scalars(
        select(SpecialtyQueueAccess.extra_specialty_id).where(
            SpecialtyQueueAccess.specialty_id == specialty.id
        )
    )
    return QueueScope(frozenset({specialty.id, *extra}))


async def ensure_can_take(
    session: AsyncSession,
    consultation: Consultation,
    *,
    specialty_id: uuid.UUID | None,
    is_admin: bool,
) -> None:
    """403 si el caso no está en ninguna de las colas de quien lo pide."""
    scope = await queue_scope(session, specialty_id=specialty_id, is_admin=is_admin)
    if not scope.allows(consultation.specialty_id):
        raise ForbiddenError("Este caso no corresponde a tu especialidad.")
