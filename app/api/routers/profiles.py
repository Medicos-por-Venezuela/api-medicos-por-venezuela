"""Endpoints REST de solo lectura para profiles (médicos / staff)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.crud import profiles as crud_profiles
from app.db.session import get_db
from app.schemas.profile import ProfileRead

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileRead])
def list_profiles(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    role: str | None = Query(None),
    db: Session = Depends(get_db),
) -> list[ProfileRead]:
    return crud_profiles.list_all(db, skip=skip, limit=limit, role=role)


@router.get("/{profile_id}", response_model=ProfileRead)
def get_profile(profile_id: uuid.UUID, db: Session = Depends(get_db)) -> ProfileRead:
    profile = crud_profiles.get(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Perfil no encontrado.")
    return profile
