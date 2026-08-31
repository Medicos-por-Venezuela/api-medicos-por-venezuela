"""ORM model for public.interconsultation_requests — segunda opinión ASÍNCRONA.

Ver tasks/interconsulta-asincrona/spec.md. El médico tratante registra a un paciente de su
consultorio (que NO está en la plataforma ni pasa por la cola) y pide ayuda a un especialista,
por especialidad o a un médico concreto. El primero que la toma gana; el contacto entre médicos
ocurre FUERA de la plataforma (WhatsApp/correo).

No confundir con `interconsultations` (segunda opinión EN VIVO durante una consulta activa, con
video compartido), ni con los dos flujos de agenda. Son cuatro cosas distintas: ver
`.knowledge/interconsultas.md`.

Máquina de estados:

    open --(el especialista TOMA)--> taken --(el TRATANTE cierra)--> closed
     |
     +--(el tratante cancela)--> cancelled

`closed` es exclusivo del médico tratante: el especialista no cierra ni suelta un caso.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base

# Estados válidos (espejo de ck_interconsultation_requests_status).
REQUEST_STATUSES = {"open", "taken", "closed", "cancelled"}

# Modos de solicitud (espejo de ck_interconsultation_requests_mode). 'specialty' es el
# principal: difunde a todos los médicos de la especialidad.
REQUEST_MODES = {"specialty", "doctor"}


class InterconsultationRequest(Base):
    __tablename__ = "interconsultation_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # El caso. Un mismo paciente puede acumular varias solicitudes con el tiempo.
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    # users.id del médico TRATANTE: el único que puede cancelar y cerrar.
    requesting_doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String, nullable=False)
    # Se guarda también en modo 'doctor' (derivada del destinatario), para que la bandeja y las
    # métricas no tengan que ramificar por modo.
    specialty_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("specialties.id", ondelete="RESTRICT"), nullable=False
    )
    # Destinatario único en modo 'doctor'; NULL en modo 'specialty'. La BD lo exige
    # (ck_interconsultation_requests_target), no solo Pydantic.
    target_doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Lo único que ve el especialista antes de tomar (junto con la edad del paciente).
    # Que no lleve PII lo garantiza el schema de salida, no esta columna.
    chief_complaint: Mapped[str] = mapped_column(Text, nullable=False)
    clinical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'open'"))
    # Un solo especialista por caso: de ahí la carrera con with_for_update(nowait=True).
    taken_by_doctor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Todavía no se muestra en ningún lado: se guarda para el historial de la próxima iteración.
    closing_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cuántos correos salieron en el fan-out: distingue "no le llegó" de "no era destinatario".
    notified_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
