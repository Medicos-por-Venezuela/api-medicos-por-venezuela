"""ORM model for public.specialties."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class Specialty(Base):
    __tablename__ = "specialties"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1000"))
    # Reserva de salud mental, en la BD y no en literales del código (ver la migración
    # 20260813_142814): `is_mental_health` = atiende salud mental; `mental_health_only` = SOLO
    # atiende salud mental (Psicología, que no es médico). El segundo implica el primero.
    is_mental_health: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    mental_health_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # ¿Se puede PEDIR en una interconsulta asíncrona? false para las que no son especialidad en
    # el sentido del feature (Medicina general): pedirle ayuda a otro general no resuelve nada.
    # En la BD y no en un literal, por lo mismo que los flags de arriba (ver 20260831_174358).
    available_for_interconsultation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
