#!/usr/bin/env python
"""Siembra datos de PRUEBA en el Supabase LOCAL para revisar el módulo Marketing.

Crea fichas demo en `doctors` y respuestas demo en `marketing_survey_responses` (correos
`@demo.example.com`) para ver en /admin/marketing: la columna "Profesional" (cruce por correo),
su ficha en el modal y, en Especialistas, la columna y el filtro de especialidad. Una de las
respuestas no tiene ficha a propósito: es la fila que enseña el "—".

Idempotente: borra lo que este mismo script sembró (mismo dominio de correo) y lo vuelve a
crear. Aborta si el entorno no es `development` o si la conexión no es local: esto nunca debe
correr contra Supabase producción.

Uso:
  uv run python scripts/seed_marketing_demo.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

ROOT = Path(__file__).resolve().parent.parent
# Permite `import src...` aunque el paquete no esté instalado en modo editable.
sys.path.insert(0, str(ROOT))

# Evita UnicodeEncodeError en consolas Windows con codepage no-UTF8 (cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

# Los imports de `src` van después de tocar `sys.path` (patrón de scripts/migrate.py), por eso
# llevan `noqa: E402`.
from src.core.config import settings  # noqa: E402
from src.db.session import AsyncSessionLocal  # noqa: E402
from src.models.doctor import Doctor  # noqa: E402
from src.models.marketing_survey_response import MarketingSurveyResponse  # noqa: E402
from src.models.professional_type import ProfessionalType  # noqa: E402
from src.models.specialty import Specialty  # noqa: E402

DEMO_DOMAIN = "demo.example.com"
DEMO_LIKE = f"%@{DEMO_DOMAIN}"

# Ficha demo -> correo @demo.example.com. El pediatra va con mayúsculas a propósito: el cruce
# del backend va por lower(email), así que su respuesta (en minúsculas) tiene que encontrarlo.
DEMO_DOCTORS = (
    {
        "full_name": "Dra. Valentina Ríos",
        "email": "demo.cardio@demo.example.com",
        "specialty": "Cardiología",
        "professional_type": "Médico",
        "cedula": "V-99900001",
        "license": "MPPS-99901",
        "phone": "+58412000901",
        "country_of_residence": "Venezuela",
    },
    {
        "full_name": "Dr. Andrés Beltrán",
        "email": "Demo.Pediatra@Demo.Example.com",
        "specialty": "Pediatría y subespecialidades",
        "professional_type": "Médico",
        "cedula": "V-99900002",
        "license": "MPPS-99902",
        "phone": "+58412000902",
        "country_of_residence": "Colombia",
    },
    {
        "full_name": "Dra. Camila Duarte",
        "email": "demo.dermato@demo.example.com",
        "specialty": "Dermatología",
        "professional_type": "Médico",
        "cedula": "V-99900003",
        "license": "MPPS-99903",
        "phone": "+58412000903",
        "country_of_residence": "España",
    },
    {
        "full_name": "Lcda. Sofía Marín",
        "email": "demo.psico@demo.example.com",
        "specialty": "Psicología",
        "professional_type": "Psicólogo",
        "cedula": "V-99900004",
        "license": "FPV-99904",
        "phone": "+58412000904",
        "country_of_residence": "Venezuela",
    },
    {
        "full_name": "Dr. José Rangel",
        "email": "demo.general@demo.example.com",
        "specialty": "Medicina general",
        "professional_type": "Médico",
        "cedula": "V-99900005",
        "license": "MPPS-99905",
        "phone": "+58412000905",
        "country_of_residence": "Venezuela",
    },
)

# Una respuesta por encuesta y correo (la clave única del upsert del formulario). Los códigos y
# las zonas son los de las encuestas reales; `demo.sin-ficha@…` no existe en `doctors`.
DEMO_RESPONSES = (
    {
        "survey": "especialistas",
        "email": "demo.cardio@demo.example.com",
        "roles": ["atender_pacientes", "responder_interconsultas"],
        "moments": ["manana", "noche"],
        "days": ["lunes", "miercoles", "viernes"],
        "weekly_hours": "entre_3_y_6",
        "availability_notes": "Prefiero las consultas por la mañana.",
        "timezone": "venezuela",
        "notes": "Puedo empezar la próxima semana.",
    },
    {
        "survey": "especialistas",
        "email": "demo.pediatra@demo.example.com",
        "roles": ["atender_y_responder"],
        "moments": ["tarde"],
        "days": ["martes", "jueves"],
        "weekly_hours": "mas_de_6",
        "timezone": "colombia_peru_ecuador",
        "timezone_other": "Bogotá",
        "notes": "Atiendo niños desde recién nacidos.",
    },
    {
        "survey": "especialistas",
        "email": "demo.dermato@demo.example.com",
        "roles": ["coordinar_especialidad", "otra"],
        "role_other_detail": "Charlas de cuidado de la piel para pacientes",
        "moments": ["variable"],
        "days": ["variable"],
        "weekly_hours": "menos_de_1",
        "timezone": "espana_italia_francia_alemania",
        "availability_notes": "Puedo los fines de semana.",
    },
    {
        "survey": "especialistas",
        "email": "demo.sin-ficha@demo.example.com",
        "roles": ["responder_interconsultas"],
        "moments": ["noche"],
        "days": ["sabado"],
        "weekly_hours": "entre_1_y_3",
        "timezone": "venezuela",
        "notes": "Respondo desde un correo que no está en la ficha de médicos.",
    },
    {
        "survey": "psicologos",
        "email": "demo.psico@demo.example.com",
        "roles": ["atender_pacientes", "rol_activo"],
        "role_active_detail": "Coordinar al equipo de psicología",
        "moments": ["noche"],
        "days": ["lunes", "sabado"],
        "weekly_hours": "entre_1_y_3",
        "timezone": "venezuela",
        "availability_notes": "Después de las 6 pm.",
    },
    {
        "survey": "psicologos",
        "email": "demo.sin-ficha@demo.example.com",
        "roles": ["otra"],
        "role_other_detail": "Grupos de apoyo para cuidadores",
        "moments": ["variable"],
        "days": ["domingo"],
        "weekly_hours": "menos_de_1",
        "timezone": "otra",
        "timezone_other": "Japón (GMT+9)",
    },
    {
        "survey": "medicos-generales",
        "email": "demo.general@demo.example.com",
        "roles": ["atender_pacientes", "rol_activo"],
        "role_active_detail": "Coordinar guardias de la red",
        "moments": ["manana", "tarde"],
        "days": ["lunes", "martes", "miercoles"],
        "weekly_hours": "mas_de_6",
        "availability_notes": "Disponible de lunes a miércoles.",
    },
    {
        "survey": "medicos-generales",
        "email": "demo.sin-ficha@demo.example.com",
        "roles": ["pedir_interconsultas"],
        "moments": ["noche"],
        "days": ["jueves", "viernes"],
        "weekly_hours": "entre_1_y_3",
    },
)


def _assert_local() -> None:
    """Corta si la conexión no es el Supabase local: estos datos son de PRUEBA."""
    host = settings.DATABASE_URL or settings.POSTGRES_HOST
    if settings.ENVIRONMENT != "development" or not any(
        marker in host for marker in ("localhost", "127.0.0.1", "host.docker.internal")
    ):
        raise SystemExit(
            "Este script siembra datos de PRUEBA y solo corre contra el Supabase LOCAL. "
            f"Ahora: ENVIRONMENT={settings.ENVIRONMENT}, host={host}. Abortado."
        )


async def _catalog_ids(session: AsyncSession, model, names: set[str]) -> dict[str, uuid.UUID]:
    """Id por nombre (sin distinguir mayúsculas) de un catálogo vivo. Si falta un nombre, corta
    con el aviso: en una base recién creada hay que sembrar el catálogo antes."""
    rows = await session.execute(select(model.name, model.id).where(model.deleted_at.is_(None)))
    by_name = {name.lower(): item_id for name, item_id in rows.tuples()}
    missing = sorted(name for name in names if name.lower() not in by_name)
    if missing:
        raise SystemExit(
            f"Faltan en el catálogo local: {missing}. Créalas en el panel "
            "(/admin/especialidades y /admin/tipos-profesionales) y vuelve a correr."
        )
    return {name: by_name[name.lower()] for name in names}


async def _clean(session: AsyncSession) -> None:
    """Borra la siembra anterior (mismo dominio demo), para que correr dos veces no duplique."""
    await session.execute(
        delete(MarketingSurveyResponse).where(MarketingSurveyResponse.email.like(DEMO_LIKE))
    )
    await session.execute(delete(Doctor).where(func.lower(Doctor.email).like(DEMO_LIKE)))


async def seed() -> None:
    _assert_local()
    async with AsyncSessionLocal() as session:
        await _clean(session)
        specialty_ids = await _catalog_ids(
            session, Specialty, {doctor["specialty"] for doctor in DEMO_DOCTORS}
        )
        type_ids = await _catalog_ids(
            session, ProfessionalType, {doctor["professional_type"] for doctor in DEMO_DOCTORS}
        )
        for data in DEMO_DOCTORS:
            session.add(
                Doctor(
                    full_name=data["full_name"],
                    email=data["email"],
                    cedula=data["cedula"],
                    license=data["license"],
                    phone=data["phone"],
                    country_of_residence=data["country_of_residence"],
                    specialty_id=specialty_ids[data["specialty"]],
                    professional_type_id=type_ids[data["professional_type"]],
                    status=1,
                    verified=True,
                )
            )
        # Fechas escalonadas: la lista ordena por `updated_at` desc, así se ve un orden variado.
        now = datetime.now(UTC)
        for index, data in enumerate(DEMO_RESPONSES):
            session.add(
                MarketingSurveyResponse(
                    **data,
                    created_at=now - timedelta(days=2, hours=index),
                    updated_at=now - timedelta(hours=index),
                )
            )
        await session.commit()

    print(
        f"Sembrados {len(DEMO_DOCTORS)} profesionales demo y {len(DEMO_RESPONSES)} respuestas "
        f"demo en {DEMO_DOMAIN}. Refresca /admin/marketing."
    )


if __name__ == "__main__":
    asyncio.run(seed())
