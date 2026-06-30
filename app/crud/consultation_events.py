"""Operaciones CRUD para consultation_events."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.consultation_event import ConsultationEvent
from app.schemas.consultation_event import ConsultationEventCreate


def list_by_consultation(db: Session, consultation_id: uuid.UUID) -> list[ConsultationEvent]:
    stmt = (
        select(ConsultationEvent)
        .where(ConsultationEvent.consultation_id == consultation_id)
        .order_by(ConsultationEvent.created_at.asc())
    )
    return list(db.scalars(stmt).all())


def create(db: Session, data: ConsultationEventCreate) -> ConsultationEvent:
    event = ConsultationEvent(**data.model_dump())
    db.add(event)
    db.commit()
    db.refresh(event)
    return event
