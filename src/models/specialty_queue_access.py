"""ORM model for public.specialty_queue_access."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class SpecialtyQueueAccess(Base):
    """Cola que los médicos de `specialty_id` ven además de la suya (p. ej. Psiquiatría →
    Psicología). La cola es por especialidad exacta; esto son las excepciones del equipo,
    sembradas en la migración 20260917_114058."""

    __tablename__ = "specialty_queue_access"

    specialty_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("specialties.id", ondelete="CASCADE"), primary_key=True
    )
    extra_specialty_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("specialties.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
