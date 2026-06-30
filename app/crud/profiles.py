"""Operaciones de lectura para profiles."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.profile import Profile


def get(db: Session, profile_id: uuid.UUID) -> Profile | None:
    return db.get(Profile, profile_id)


def list_all(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    role: str | None = None,
) -> list[Profile]:
    stmt = select(Profile)
    if role:
        stmt = stmt.where(Profile.role == role)
    stmt = stmt.order_by(Profile.created_at.desc()).offset(skip).limit(limit)
    return list(db.scalars(stmt).all())
