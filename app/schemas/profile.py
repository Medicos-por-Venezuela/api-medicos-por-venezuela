"""Esquemas Pydantic para profiles."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


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


class ProfileRead(ProfileBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    last_seen_at: datetime | None = None
    created_at: datetime
