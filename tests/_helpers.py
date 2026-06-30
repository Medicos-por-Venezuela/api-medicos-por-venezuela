"""Utilidades compartidas por las pruebas (firma de JWT de Supabase para auth)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from src.core.config import settings
from src.models.profile import Profile


def make_token(sub: uuid.UUID | str) -> str:
    """Firma un JWT tipo Supabase (HS256) para el usuario `sub`."""
    return jwt.encode(
        {
            "sub": str(sub),
            "aud": settings.SUPABASE_JWT_AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        settings.SUPABASE_JWT_SECRET,
        algorithm=settings.SUPABASE_JWT_ALGORITHM,
    )


def auth_headers(sub: uuid.UUID | str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub)}"}


def make_profile(role: str = "doctor", specialty: str | None = None) -> Profile:
    """Crea (sin persistir) un perfil de staff activo y verificado para pruebas."""
    return Profile(
        id=uuid.uuid4(),
        full_name=f"Test {role}",
        role=role,
        specialty=specialty,
        active=True,
        verified=True,
        role_chosen=True,
    )
