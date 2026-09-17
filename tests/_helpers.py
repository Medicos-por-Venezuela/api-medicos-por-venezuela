"""Utilidades compartidas por las pruebas (firma de JWT de Supabase para auth)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.doctor import Doctor
from src.models.profile import Profile
from src.models.specialty import Specialty

# La cola es por especialidad exacta: un médico sin especialidad no ve ni toma ningún caso. Las
# pruebas que no tratan de especialidades usan esta para médicos y consultas por igual.
GENERAL = "Medicina general"


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
    if specialty is not None:
        # La cola decide con la FK (`users.specialty_id`), no con el nombre.
        profile.specialty_id = await specialty_id_by_name(session, specialty)
    session.add(profile)
    await session.flush()
    doctor_overrides.setdefault("specialty_id", profile.specialty_id)
    session.add(make_doctor_row(profile.id, verified=verified, **doctor_overrides))
    await session.flush()
    return profile


async def specialty_id_by_name(session: AsyncSession, name: str) -> uuid.UUID:
    """Id de una especialidad viva del catálogo por nombre (sin distinguir mayúsculas)."""
    return (
        await session.execute(
            select(Specialty.id).where(
                func.lower(Specialty.name) == name.lower(), Specialty.deleted_at.is_(None)
            )
        )
    ).scalar_one()


async def any_specialty_id(client: AsyncClient) -> str:
    """Id de Medicina general, para crear consultas en las pruebas.

    `specialty_id` es obligatorio en `ConsultationCreate` (esa columna ES el matching de la cola),
    así que ya no se puede crear una consulta sin él. Es la misma especialidad que `GENERAL`, la
    de los médicos de las pruebas: con la cola por especialidad exacta, un caso de otra no lo
    vería ni lo podría tomar el médico de la prueba sin que eso sea lo que se prueba.
    """
    resp = await client.get("/api/v1/specialties")
    return next(s["id"] for s in resp.json() if s["name"].lower() == GENERAL.lower())
