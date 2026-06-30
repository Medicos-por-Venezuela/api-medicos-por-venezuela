"""Modelo ORM de la tabla public.doctors."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    specialty: Mapped[str] = mapped_column(String, nullable=False)
    country: Mapped[str] = mapped_column(String, nullable=False)
    phone_whatsapp: Mapped[str] = mapped_column(String, nullable=False)
    preferred_platform: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'google_meet'")
    )
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    cmv_number: Mapped[str | None] = mapped_column(String, nullable=True)
    msds_number: Mapped[str | None] = mapped_column(String, nullable=True)
    sanidad_number: Mapped[str | None] = mapped_column(String, nullable=True)
    colegio_number: Mapped[str | None] = mapped_column(String, nullable=True)
    foreign_registration: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    # En producción user_id referencia auth.users(id).
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
