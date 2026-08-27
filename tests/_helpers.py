"""Utilidades compartidas por las pruebas (firma de JWT de Supabase para auth)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.doctor import Doctor
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
    """Crea (sin persistir) un perfil de staff activo y verificado para pruebas.

    ⚠️ Para un médico esto NO basta: sin ficha habilitada en `doctors` el principal se
    queda sin permisos (ver `doctors.has_valid_credential`). Usa `add_doctor`."""
    return Profile(
        id=uuid.uuid4(),
        full_name=f"Test {role}",
        role=role,
        specialty=specialty,
        active=True,
        verified=True,
        role_chosen=True,
    )


def make_doctor_row(user_id: uuid.UUID, *, verified: bool = True, **overrides) -> Doctor:
    """Ficha en `doctors` (sin persistir) habilitada para atender: verificada, con cédula
    y licencia. `overrides` permite romper justo un requisito en los tests del gate."""
    fields = {
        # Cédula única por ficha: el índice parcial `uq_doctors_cedula_not_deleted` la exige
        # y varios tests crean varios médicos en la misma transacción.
        "cedula": f"V-{uuid.uuid4().int % 10**8:08d}",
        "full_name": "Test Doctor",
        "license": "MPPS-12345",
        "status": 1,
    }
    fields.update(overrides)
    return Doctor(user_id=user_id, verified=verified, **fields)


async def add_doctor(
    session: AsyncSession,
    role: str = "doctor",
    specialty: str | None = None,
    *,
    verified: bool = True,
    **doctor_overrides,
) -> Profile:
    """Persiste un médico COMPLETO: su cuenta en `users` + su ficha habilitada en `doctors`.

    Es lo que hace falta para que un JWT de médico pase el gate de credencial y conserve
    sus permisos. Los `doctor_overrides` (o `verified=False`) sirven para construir el
    médico *no* habilitado en los tests del propio gate."""
    profile = make_profile(role=role, specialty=specialty)
    session.add(profile)
    await session.flush()
    session.add(make_doctor_row(profile.id, verified=verified, **doctor_overrides))
    await session.flush()
    return profile
