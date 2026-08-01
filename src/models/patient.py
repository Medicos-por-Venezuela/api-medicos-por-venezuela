"""Modelo ORM de la tabla public.patients."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    phone_whatsapp: Mapped[str] = mapped_column(String, nullable=False)
    affected_zone: Mapped[str] = mapped_column(String, nullable=False)
    needs_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # En producción user_id referencia auth.users(id) (cuenta opcional del paciente).
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    age_range: Mapped[str | None] = mapped_column(String, nullable=True)
    cedula: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    allergies: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Carga familiar: un menor referencia a su adulto responsable (otra fila de
    # patients). parentesco solo aplica cuando parent_id está seteado.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    parentesco: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Soft delete: "borrar" un paciente = marcar esto (nunca hard delete). Las listas lo filtran.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    consultations: Mapped[list["Consultation"]] = relationship(  # noqa: F821
        back_populates="patient", cascade="all, delete-orphan"
    )
