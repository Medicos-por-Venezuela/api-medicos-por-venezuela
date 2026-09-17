"""ORM model for public.doctor_specialties."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class DoctorSpecialty(Base):
    """Especialidad que ejerce un médico. Un médico puede tener varias (internista y cardiólogo,
    p. ej.) y su cola es la unión de todas. `users.specialty_id` sigue siendo la PRINCIPAL: la
    usan el pool, los reportes, el admin y la bandeja de interconsultas."""

    __tablename__ = "doctor_specialties"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    specialty_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("specialties.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
