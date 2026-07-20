"""Pruebas del dashboard de estadísticas admin (`GET /stats/dashboard`).

La BD local tiene datos de prod restaurados (miles de doctors/patients/consultations
ya committeados), así que los conteos se comprueban por DELTA: se llama al servicio
antes y después de sembrar filas conocidas y se compara el incremento exacto, en vez
de asertar totales absolutos.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consultation import Consultation
from src.models.doctor import Doctor
from src.models.patient import Patient
from src.models.rbac import Permission, Role, RolePermission
from src.services import stats as stats_service
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
STATS_READ_MIGRATION = "20260720_105744_seed_stats_read_permission.sql"


async def _stats(db: AsyncSession) -> stats_service.StatsResponse:
    return await stats_service.get_dashboard_stats(db)


# --- Conteos (delta sobre la base existente) --------------------------------


async def test_dashboard_stats_counts_doctors_and_patients(db_session: AsyncSession) -> None:
    before = await _stats(db_session)

    online_prof = make_profile(role="doctor")
    online_prof.last_seen_at = datetime.now(UTC) - timedelta(minutes=1)
    offline_prof = make_profile(role="doctor")
    offline_prof.last_seen_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.add_all([online_prof, offline_prof])
    await db_session.flush()

    db_session.add_all(
        [
            Doctor(full_name="Dr Online Stats", user_id=online_prof.id, status=1),
            Doctor(full_name="Dr Offline Stats", user_id=offline_prof.id, status=1),
            Doctor(full_name="Dr Baja Stats", user_id=None, status=0),
        ]
    )
    db_session.add(
        Patient(
            full_name="Paciente Stats",
            phone_whatsapp="+58412000000",
            affected_zone="Caracas",
            consent=True,
        )
    )
    await db_session.flush()

    after = await _stats(db_session)

    # Solo los 2 status=1 cuentan como registrados; el de baja (status=0) no.
    assert after.doctors_registered == before.doctors_registered + 2
    # Solo el que tiene last_seen_at < 3 min cuenta como online.
    assert after.doctors_online == before.doctors_online + 1
    assert after.patients_registered == before.patients_registered + 1


async def test_dashboard_stats_online_window_boundary(db_session: AsyncSession) -> None:
    """Un heartbeat de 2 min cuenta como online; uno de 5 min, no (ventana de 3 min)."""
    before = await _stats(db_session)

    fresh_prof = make_profile(role="doctor")
    fresh_prof.last_seen_at = datetime.now(UTC) - timedelta(minutes=2)
    stale_prof = make_profile(role="doctor")
    stale_prof.last_seen_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.add_all([fresh_prof, stale_prof])
    await db_session.flush()

    db_session.add_all(
        [
            Doctor(full_name="Dr Fresh Stats", user_id=fresh_prof.id, status=1),
            Doctor(full_name="Dr Stale Stats", user_id=stale_prof.id, status=1),
        ]
    )
    await db_session.flush()

    after = await _stats(db_session)

    assert after.doctors_registered == before.doctors_registered + 2
    assert after.doctors_online == before.doctors_online + 1


async def test_dashboard_stats_counts_consultations_by_bucket(db_session: AsyncSession) -> None:
    patient = Patient(
        full_name="Paciente Consultas Stats",
        phone_whatsapp="+58412000001",
        affected_zone="Caracas",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()

    before = await _stats(db_session)

    db_session.add_all(
        [
            Consultation(
                patient_id=patient.id, status="waiting", entered_call_at=datetime.now(UTC)
            ),
            Consultation(patient_id=patient.id, status="waiting"),  # sin entered_call_at
            Consultation(patient_id=patient.id, status="in_progress"),
            Consultation(patient_id=patient.id, status="referred_to_specialist"),
            Consultation(patient_id=patient.id, status="urgent_in_person"),
            Consultation(patient_id=patient.id, status="patient_no_show"),
            Consultation(patient_id=patient.id, status="cancelled"),
            Consultation(patient_id=patient.id, status="closed"),
            Consultation(patient_id=patient.id, status="closed_by_admin"),
            # "contacted_whatsapp" está en CONSULTATION_STATUSES y en el CHECK de la
            # base; se inserta directo por ORM (no pasa por el endpoint de creación,
            # que llama a _validate_status) porque este test solo ejercita stats.
            Consultation(patient_id=patient.id, status="contacted_whatsapp"),
        ]
    )
    await db_session.flush()

    after = await _stats(db_session)

    # Solo 1 de las 2 'waiting' tiene entered_call_at.
    assert after.consultations_waiting == before.consultations_waiting + 1
    # in_progress + referred_to_specialist + urgent_in_person + patient_no_show +
    # cancelled + contacted_whatsapp (urgent_in_person también pertenece a su
    # propio KPI, además de este bucket amplio "en progreso").
    assert after.consultations_in_progress == before.consultations_in_progress + 6
    # closed + closed_by_admin.
    assert after.consultations_closed == before.consultations_closed + 2
    # Solo urgent_in_person.
    assert after.consultations_urgent == before.consultations_urgent + 1


# --- Endpoint / autorización --------------------------------------------------


async def test_dashboard_stats_endpoint_returns_all_fields_for_admin(
    client: AsyncClient,
) -> None:
    resp = await client.get(f"{PREFIX}/stats/dashboard")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "doctors_registered",
        "doctors_online",
        "patients_registered",
        "consultations_waiting",
        "consultations_in_progress",
        "consultations_closed",
        "consultations_urgent",
    }
    assert all(isinstance(v, int) for v in body.values())


async def test_dashboard_stats_endpoint_for_super_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    super_admin = make_profile(role="super_admin")
    db_session.add(super_admin)
    await db_session.flush()
    resp = await client.get(f"{PREFIX}/stats/dashboard", headers=auth_headers(super_admin.id))
    assert resp.status_code == 200, resp.text


async def test_dashboard_stats_requires_permission(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient = make_profile(role="patient")  # patient no tiene 'stats.read'
    db_session.add(patient)
    await db_session.flush()
    resp = await client.get(f"{PREFIX}/stats/dashboard", headers=auth_headers(patient.id))
    assert resp.status_code == 403


# --- Migración idempotente ----------------------------------------------------


def _statements(sql: str) -> list[str]:
    """Divide un archivo de migración en sus statements individuales (por ';').

    El driver asyncpg (protocolo extendido) rechaza varios comandos en un solo
    `execute()`; el runner real usa el protocolo simple de asyncpg directo, pero
    aquí probamos idempotencia statement-por-statement con la misma sesión de test.
    Los comentarios `--` se eliminan ANTES de partir por ';' (un comentario puede
    contener un ';' en su propio texto, como en el stub de `make:migration`).
    """
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in without_comments.split(";") if s.strip()]


async def _apply_migration(db: AsyncSession, filename: str) -> None:
    sql = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
    for statement in _statements(sql):
        await db.execute(text(statement))


async def test_stats_read_migration_is_idempotent_and_grants_both_roles(
    db_session: AsyncSession,
) -> None:
    await _apply_migration(db_session, STATS_READ_MIGRATION)
    await _apply_migration(db_session, STATS_READ_MIGRATION)  # re-aplicar: debe ser no-op

    grants = (
        (
            await db_session.execute(
                select(Role.code)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(Permission.code == "stats.read")
            )
        )
        .scalars()
        .all()
    )
    assert sorted(grants) == ["admin", "super_admin"]


async def test_stats_read_permission_exists_exactly_once(db_session: AsyncSession) -> None:
    await _apply_migration(db_session, STATS_READ_MIGRATION)
    await _apply_migration(db_session, STATS_READ_MIGRATION)

    count = await db_session.scalar(select(Permission.id).where(Permission.code == "stats.read"))
    assert count is not None
    all_matches = (
        (await db_session.execute(select(Permission).where(Permission.code == "stats.read")))
        .scalars()
        .all()
    )
    assert len(all_matches) == 1
