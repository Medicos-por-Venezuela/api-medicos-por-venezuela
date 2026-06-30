"""Modelo ORM de la tabla public.consultations."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base

# Estados permitidos según consultations_status_check.
CONSULTATION_STATUSES = {
    "waiting",
    "in_progress",
    "referred_to_specialist",
    "urgent_in_person",
    "closed",
    "cancelled",
    "patient_no_show",
    "closed_by_admin",
}


class Consultation(Base):
    __tablename__ = "consultations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("doctors.id"), nullable=True
    )
    assigned_doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'waiting'"))
    priority: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'normal'"))
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    chief_complaint: Mapped[str | None] = mapped_column(Text, nullable=True)
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    referred_specialty: Mapped[str | None] = mapped_column(String, nullable=True)
    platform_used: Mapped[str | None] = mapped_column(String, nullable=True)
    meeting_link: Mapped[str | None] = mapped_column(String, nullable=True)
    video_room_url: Mapped[str | None] = mapped_column(String, nullable=True)
    doctor_license_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    has_prescription: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    has_referral: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    has_rest_note: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    follow_up_scheduled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    contacted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    patient_last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    patient: Mapped["Patient"] = relationship(back_populates="consultations")  # noqa: F821
    events: Mapped[list["ConsultationEvent"]] = relationship(  # noqa: F821
        back_populates="consultation", cascade="all, delete-orphan"
    )
