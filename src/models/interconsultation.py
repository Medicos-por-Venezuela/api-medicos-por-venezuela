"""ORM model for public.interconsultations — segunda opinión EN TIEMPO REAL.

Ver .knowledge/interconsultas.md. El médico que atiende invita a UN médico del pool durante una
consulta ACTIVA (sigue abierta); ambos comparten el video. El invitado ve datos limitados (motivo,
notas, edad). No confundir con "Agendar con Especialista" (que cierra la consulta y agenda para
otro día).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class Interconsultation(Base):
    __tablename__ = "interconsultations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Consulta inter-consultada. Único (uq_interconsultations_consultation): 1 por consulta.
    consultation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("consultations.id", ondelete="CASCADE"), nullable=False
    )
    # user_id (profiles.id) del médico INVITADO (ve datos limitados).
    invited_doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # user_id del médico que ATIENDE la consulta (quien invita).
    created_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
