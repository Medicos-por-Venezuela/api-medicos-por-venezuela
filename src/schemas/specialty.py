"""Pydantic schemas for specialties."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SpecialtyCatalogResponse(BaseModel):
    """Matching catalog used by the frontend and queue logic."""

    specialties: list[str]
    needs: list[str]
    specialty_needs: dict[str, list[str]]
    reserved_needs: dict[str, list[str]]


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
