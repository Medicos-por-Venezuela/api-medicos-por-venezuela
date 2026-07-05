"""Esquemas Pydantic para patients (Create / Update / Response)."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class PatientBase(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=200)
    phone_whatsapp: str = Field(..., min_length=5, max_length=30)
    affected_zone: str = Field(..., min_length=2, max_length=100)
    cedula: str | None = Field(default=None, max_length=20)
    age_range: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    needs_tags: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=20
    )
    description: str | None = Field(default=None, max_length=2000)
    user_id: uuid.UUID | None = None
    allergies: str | None = Field(default=None, max_length=500)
    # Carga familiar: si parent_id viene, este registro es un menor a cargo de otro
    # patient (el adulto responsable); parentesco describe esa relación.
    parent_id: uuid.UUID | None = None
    parentesco: str | None = Field(default=None, max_length=50)


class PatientCreate(PatientBase):
    model_config = ConfigDict(extra="forbid")

    # El insert exige consentimiento (ver política RLS patients_insert_public).
    consent: bool = True

    @model_validator(mode="after")
    def _parentesco_requiere_parent_id(self) -> "PatientCreate":
        if (self.parent_id is None) != (self.parentesco is None):
            raise ValueError("parent_id y parentesco deben venir juntos, o ninguno de los dos.")
        return self


class PatientUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2)
    phone_whatsapp: str | None = Field(default=None, min_length=5)
    affected_zone: str | None = Field(default=None, min_length=2)
    cedula: str | None = None
    age_range: str | None = None
    email: EmailStr | None = None
    needs_tags: list[str] | None = None
    description: str | None = None
    allergies: str | None = None
    parent_id: uuid.UUID | None = None
    parentesco: str | None = None


class PatientResponse(PatientBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    consent: bool
    consent_at: datetime | None = None
    created_at: datetime
