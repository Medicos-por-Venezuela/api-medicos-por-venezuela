"""Clase base declarativa de SQLAlchemy."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base común para todos los modelos ORM."""

    pass
