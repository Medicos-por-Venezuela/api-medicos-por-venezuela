"""ORM model for public.marketing_survey_responses — respuestas a las encuestas de marketing.

Tres encuestas de re-targeting (psicólogos, especialistas, médicos generales) que se mandan por
correo masivo. Una fila por encuesta y correo: responder de nuevo actualiza la fila (ver
`services/marketing.py::submit_response`, que hace el upsert).

El correo NO está verificado —llega en el enlace del correo masivo—, así que esta tabla no se liga
a `users`/`doctors` ni decide nada de acceso.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class MarketingSurveyResponse(Base):
    __tablename__ = "marketing_survey_responses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Slug de la encuesta (espejo de ck_marketing_survey_responses_survey).
    survey: Mapped[str] = mapped_column(String, nullable=False)
    # Normalizado (trim + minúsculas): es la mitad de la clave única del upsert.
    email: Mapped[str] = mapped_column(String, nullable=False)
    # Códigos de opción, no el texto del formulario (las etiquetas las resuelve el servicio).
    roles: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    role_active_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    role_other_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    moments: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    days: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    weekly_hours: Mapped[str | None] = mapped_column(String, nullable=True)
    availability_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    timezone_other: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Primera respuesta.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Última vez que respondió. El upsert la fija a mano (un `onupdate` del ORM no corre en un
    # `INSERT ... ON CONFLICT DO UPDATE` construido con `insert()`).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
