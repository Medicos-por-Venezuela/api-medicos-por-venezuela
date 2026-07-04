"""Modelo ORM de la tabla public.users (antes 'profiles')."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base

# Roles permitidos según profiles_role_check.
PROFILE_ROLES = {"patient", "doctor", "specialist", "admin", "super_admin"}


class Profile(Base):
    # Renombrada desde 'profiles'; una vista de compatibilidad mantiene el nombre viejo
    # para el frontend directo, el trigger de Auth y las funciones RLS.
    __tablename__ = "users"

    # En producción id referencia auth.users(id) (gestionado por Supabase Auth).
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'doctor'"))
    specialty: Mapped[str | None] = mapped_column(String, nullable=True)
    medical_license: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    role_chosen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    did_article_8: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    article_8_doc_path: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
