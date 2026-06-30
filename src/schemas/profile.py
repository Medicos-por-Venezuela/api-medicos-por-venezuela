"""Esquemas Pydantic para profiles (Response)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ProfileBase(BaseModel):
    email: EmailStr | None = None
    full_name: str
    role: str = "doctor"
    specialty: str | None = None
    medical_license: str | None = None
    country: str | None = None
    whatsapp_number: str | None = None
    verified: bool = False
    active: bool = True
    role_chosen: bool = False
    did_article_8: bool = False
    article_8_doc_path: str | None = None


class ProfileResponse(ProfileBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    last_seen_at: datetime | None = None
    created_at: datetime


class ProfileActiveRequest(BaseModel):
    """Revocar (`active=false`) o reactivar (`active=true`) un médico."""

    model_config = ConfigDict(extra="forbid")

    active: bool


class ProfileFinalizeRoleRequest(BaseModel):
    """Finalizar el rol del propio usuario (solo `patient`/`doctor`)."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(..., pattern="^(patient|doctor)$")
    specialty: str | None = None
    country: str | None = None
    medical_license: str | None = None
    whatsapp_number: str | None = None
