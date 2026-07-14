"""Modelo ORM de la tabla public.doctors (reconstruida de cero)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Vínculo 1:1 con la cuenta (users). Los médicos backfilleados/nuevos lo llevan;
    # las 3 filas mock legacy pueden tenerlo null.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    professional_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    specialty_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # cedula/phone/email son nullable: los médicos backfilleados desde users no los traen
    # (el contacto vive en users; la cédula se completa luego).
    cedula: Mapped[str | None] = mapped_column(String, nullable=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    license: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    country_of_residence: Mapped[str | None] = mapped_column(String, nullable=True)
    # 0 = se dio de baja, 1 = activo, 2 = expulsado por admin.
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    # Lo fija el backend según SACS/FPV al registrar.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
