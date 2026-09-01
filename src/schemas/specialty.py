"""Pydantic schemas for specialties."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clean_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    value = value.strip()
    if "<" in value or ">" in value:
        raise ValueError("name must not contain HTML")
    return value


class SpecialtyBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    status: str = Field(default="active", pattern="^(active|inactive)$")
    # Reserva de salud mental. Editable por admin (`catalogs.manage`): dar de alta una
    # especialidad de salud mental nueva no debe requerir un despliegue.
    is_mental_health: bool = False
    mental_health_only: bool = False
    # Igual que los de salud mental: editable por admin, para que excluir o reincorporar una
    # especialidad del selector de interconsultas sea un UPDATE y no un despliegue.
    available_for_interconsultation: bool = True

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: object) -> str:
        return _clean_name(value)


class SpecialtyCreate(SpecialtyBase):
    model_config = ConfigDict(extra="forbid")


class SpecialtyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")
    is_mental_health: bool | None = None
    mental_health_only: bool | None = None
    available_for_interconsultation: bool | None = None

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: object) -> str:
        return _clean_name(value)

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, value: object) -> object:
        if value is None:
            raise ValueError("status must not be null")
        return value


class SpecialtyResponse(SpecialtyBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
