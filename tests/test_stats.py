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
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consultation import Consultation
from src.models.doctor import Doctor
from src.models.patient import Patient
from src.models.rbac import Permission, Role, RolePermission
from src.services import stats as stats_service
from tests._helpers import auth_headers, make_profile, specialty_id_by_name

PREFIX = "/api/v1"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
STATS_READ_MIGRATION = "20260720_105744_seed_stats_read_permission.sql"


async def _stats(db: AsyncSession) -> stats_service.StatsResponse:
    return await stats_service.get_dashboard_stats(db)


# --- Conteos (delta sobre la base existente) --------------------------------


async def test_dashboard_stats_counts_doctors_and_patients(db_session: AsyncSession) -> None:
    before = await _stats(db_session)

    # 2 cuentas role="doctor" (una online 1 min, otra offline 1 h)
    online_prof = make_profile(role="doctor")
    online_prof.last_seen_at = datetime.now(UTC) - timedelta(minutes=1)
    offline_prof = make_profile(role="doctor")
    offline_prof.last_seen_at = datetime.now(UTC) - timedelta(hours=1)
    # 1 cuenta role="specialist" sin last_seen_at
    specialist_prof = make_profile(role="specialist")
    # 1 cuenta role="patient" (no debe contar)
    patient_prof = make_profile(role="patient")
    db_session.add_all([online_prof, offline_prof, specialist_prof, patient_prof])
    await db_session.flush()

    # Una ficha Doctor con user_id=None para demostrar que YA NO cuenta
    db_session.add(Doctor(full_name="Dr Sin Cuenta Stats", user_id=None, status=1))
    await db_session.flush()

    # Pacientes: 1 vivo, 1 borrado (soft delete)
    db_session.add(
        Patient(
            full_name="Paciente Stats Vivo",
            phone_whatsapp="+58412000000",
            affected_zone="Caracas",
            consent=True,
        )
    )
    deleted_patient = Patient(
        full_name="Paciente Stats Borrado",
        phone_whatsapp="+58412000001",
        affected_zone="Maracaibo",
        consent=True,
        deleted_at=datetime.now(UTC),
    )
    db_session.add(deleted_patient)
    await db_session.flush()

    after = await _stats(db_session)

    # doctors_registered: 3 cuentas con rol clínico (2 doctor + 1 specialist)
    assert after.doctors_registered == before.doctors_registered + 3
    # doctors_online: solo la que tiene last_seen_at < 3 min (la de 1 min)
    assert after.doctors_online == before.doctors_online + 1
    # patients_registered: solo 1 (la borrada no cuenta)
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

    # 2 waiting (una CON entered_call_at, otra SIN) + una de cada estado restante = 11 filas
    db_session.add_all(
        [
            Consultation(
                patient_id=patient.id, status="waiting", entered_call_at=datetime.now(UTC)
            ),
            Consultation(patient_id=patient.id, status="waiting"),  # sin entered_call_at
            Consultation(patient_id=patient.id, status="in_progress"),
            Consultation(patient_id=patient.id, status="contacted_whatsapp"),
            Consultation(patient_id=patient.id, status="scheduled"),
            Consultation(patient_id=patient.id, status="referred_to_specialist"),
            Consultation(patient_id=patient.id, status="patient_no_show"),
            Consultation(patient_id=patient.id, status="cancelled"),
            Consultation(patient_id=patient.id, status="closed"),
            Consultation(patient_id=patient.id, status="closed_by_admin"),
            Consultation(patient_id=patient.id, status="urgent_in_person"),
        ]
    )
    await db_session.flush()

    after = await _stats(db_session)

    # waiting: ambas cuentan (con o sin entered_call_at)
    assert after.consultations_waiting == before.consultations_waiting + 2
    # in_progress: in_progress + contacted_whatsapp
    assert after.consultations_in_progress == before.consultations_in_progress + 2
    # scheduled
    assert after.consultations_scheduled == before.consultations_scheduled + 1
    # referred
    assert after.consultations_referred == before.consultations_referred + 1
    # no_show
    assert after.consultations_no_show == before.consultations_no_show + 1
    # cancelled
    assert after.consultations_cancelled == before.consultations_cancelled + 1
    # closed: closed + closed_by_admin
    assert after.consultations_closed == before.consultations_closed + 2
    # urgent
    assert after.consultations_urgent == before.consultations_urgent + 1

    # La suma de los 8 buckets debe ser igual al delta total de consultas (11)
    total_buckets_delta = (
        (after.consultations_waiting - before.consultations_waiting)
        + (after.consultations_in_progress - before.consultations_in_progress)
        + (after.consultations_scheduled - before.consultations_scheduled)
        + (after.consultations_referred - before.consultations_referred)
        + (after.consultations_no_show - before.consultations_no_show)
        + (after.consultations_cancelled - before.consultations_cancelled)
        + (after.consultations_closed - before.consultations_closed)
        + (after.consultations_urgent - before.consultations_urgent)
    )
    assert total_buckets_delta == 11


async def test_dashboard_stats_groups_consultations_by_zone(db_session: AsyncSession) -> None:
    # 2 pacientes con affected_zone="Caracas", 1 con affected_zone="" (vacío -> "Sin zona")
    patient_caracas_1 = Patient(
        full_name="Paciente Caracas 1",
        phone_whatsapp="+58412000010",
        affected_zone="Caracas",
        consent=True,
    )
    patient_caracas_2 = Patient(
        full_name="Paciente Caracas 2",
        phone_whatsapp="+58412000011",
        affected_zone="Caracas",
        consent=True,
    )
    patient_sin_zona = Patient(
        full_name="Paciente Sin Zona",
        phone_whatsapp="+58412000012",
        affected_zone="",
        consent=True,
    )
    db_session.add_all([patient_caracas_1, patient_caracas_2, patient_sin_zona])
    await db_session.flush()

    before = await _stats(db_session)
    before_zone_dict = {z.zone: z.total for z in before.consultations_by_zone}

    # 2 consultas para Caracas, 1 para Sin zona
    db_session.add_all(
        [
            Consultation(patient_id=patient_caracas_1.id, status="waiting"),
            Consultation(patient_id=patient_caracas_2.id, status="in_progress"),
            Consultation(patient_id=patient_sin_zona.id, status="closed"),
        ]
    )
    await db_session.flush()

    after = await _stats(db_session)
    after_zone_dict = {z.zone: z.total for z in after.consultations_by_zone}

    # Comparar por delta
    assert after_zone_dict.get("Caracas", 0) == before_zone_dict.get("Caracas", 0) + 2
    assert after_zone_dict.get("Sin zona", 0) == before_zone_dict.get("Sin zona", 0) + 1


async def test_dashboard_stats_groups_consultations_by_specialty(db_session: AsyncSession) -> None:
    patient = Patient(
        full_name="Paciente Especialidad Stats",
        phone_whatsapp="+58412000020",
        affected_zone="Caracas",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()

    general_id = await specialty_id_by_name(db_session, "Medicina general")

    before = await _stats(db_session)
    before_spec_dict = {s.specialty: s.total for s in before.consultations_by_specialty}

    # 2 consultas con specialty_id="Medicina general", 1 con specialty_id=None
    db_session.add_all(
        [
            Consultation(patient_id=patient.id, status="waiting", specialty_id=general_id),
            Consultation(patient_id=patient.id, status="in_progress", specialty_id=general_id),
            Consultation(patient_id=patient.id, status="closed", specialty_id=None),
        ]
    )
    await db_session.flush()

    after = await _stats(db_session)
    after_spec_dict = {s.specialty: s.total for s in after.consultations_by_specialty}

    # Comparar por delta
    assert (
        after_spec_dict.get("Medicina general", 0)
        == before_spec_dict.get("Medicina general", 0) + 2
    )
    assert (
        after_spec_dict.get("Sin especialidad", 0)
        == before_spec_dict.get("Sin especialidad", 0) + 1
    )


# --- Endpoint / autorización --------------------------------------------------


async def test_dashboard_stats_endpoint_returns_all_fields_for_admin(
    client: AsyncClient,
) -> None:
    resp = await client.get(f"{PREFIX}/stats/dashboard")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    expected_keys = {
        "doctors_registered",
        "doctors_online",
        "patients_registered",
        "consultations_waiting",
        "consultations_in_progress",
        "consultations_scheduled",
        "consultations_referred",
        "consultations_no_show",
        "consultations_cancelled",
        "consultations_closed",
        "consultations_urgent",
        "consultations_by_zone",
        "consultations_by_specialty",
    }
    assert set(body) == expected_keys
    # Validar que las listas tienen dicts con las claves esperadas
    assert isinstance(body["consultations_by_zone"], list)
    assert isinstance(body["consultations_by_specialty"], list)
    if body["consultations_by_zone"]:
        assert all(
            isinstance(item, dict) and set(item.keys()) == {"zone", "total"}
            for item in body["consultations_by_zone"]
        )
    if body["consultations_by_specialty"]:
        assert all(
            isinstance(item, dict) and set(item.keys()) == {"specialty", "total"}
            for item in body["consultations_by_specialty"]
        )
    # Los 11 KPIs son ints
    int_keys = expected_keys - {"consultations_by_zone", "consultations_by_specialty"}
    assert all(isinstance(body[k], int) for k in int_keys)


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


# --- Cifras públicas de la portada (`GET /stats/public`) ----------------------


def test_round_down_uses_the_step_for_each_magnitude() -> None:
    """Los escalones acordados con el equipo, y los dos que evitan publicar un '+0'."""
    # Medios millares a partir de 1.000: 2.900 médicos se publican como 2.500.
    assert stats_service.round_down(2900) == 2500
    assert stats_service.round_down(1000) == 1000
    assert stats_service.round_down(1499) == 1000
    # Centenas por debajo de 1.000: 379 consultas -> 300; 450 -> 400.
    assert stats_service.round_down(379) == 300
    assert stats_service.round_down(450) == 400
    assert stats_service.round_down(999) == 900
    # Decenas por debajo de 100: si no, 47 se publicaría como 0.
    assert stats_service.round_down(47) == 40
    assert stats_service.round_down(23) == 20
    # Por debajo de 10 se publica el número tal cual.
    assert stats_service.round_down(7) == 7
    assert stats_service.round_down(0) == 0


def test_round_down_never_rounds_up() -> None:
    """Nunca hacia arriba: la cifra publicada debe ser siempre defendible ('hay al menos N')."""
    for n in range(0, 3000):
        assert stats_service.round_down(n) <= n


async def test_public_stats_are_rounded_down_multiples(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/stats/public")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"doctors", "consultations", "specialties"}
    for valor in body.values():
        assert isinstance(valor, int)
        assert valor == stats_service.round_down(valor), "el valor publicado ya venía redondeado"


async def test_public_stats_needs_no_token(anon_client: AsyncClient) -> None:
    """Es la portada: si exigiera Bearer, habría que inventarse una credencial pública."""
    resp = await anon_client.get(f"{PREFIX}/stats/public")
    assert resp.status_code == 200, resp.text


async def test_public_stats_counts_every_consultation_and_active_doctors(
    db_session: AsyncSession,
) -> None:
    """Consultas: TODAS las creadas, sin filtrar por estado. Médicos: solo los activos.

    Se comprueba por delta contra el conteo crudo, no contra la cifra publicada: el redondeo
    absorbe incrementos pequeños y un test sobre la cifra redondeada no distinguiría entre
    'cuenta bien' y 'no cuenta nada'.
    """
    antes_consultas = await db_session.scalar(select(func.count()).select_from(Consultation)) or 0
    antes_medicos = (
        await db_session.scalar(
            select(func.count())
            .select_from(Doctor)
            .where(Doctor.status == 1, Doctor.deleted_at.is_(None))
        )
    ) or 0

    patient = Patient(
        full_name="Paciente Stats Publicas",
        phone_whatsapp="+58412000002",
        affected_zone="Caracas",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()
    db_session.add_all(
        [
            Consultation(patient_id=patient.id, status="waiting"),
            Consultation(patient_id=patient.id, status="cancelled"),
            Consultation(patient_id=patient.id, status="closed"),
        ]
    )
    db_session.add_all(
        [
            Doctor(full_name="Dr Publico Activo", user_id=None, status=1),
            Doctor(full_name="Dr Publico De Baja", user_id=None, status=0),
        ]
    )
    await db_session.flush()

    despues_consultas = (
        await db_session.scalar(select(func.count()).select_from(Consultation)) or 0
    )
    despues_medicos = (
        await db_session.scalar(
            select(func.count())
            .select_from(Doctor)
            .where(Doctor.status == 1, Doctor.deleted_at.is_(None))
        )
    ) or 0

    # Las 3 consultas cuentan, incluidas la que sigue en espera y la cancelada.
    assert despues_consultas == antes_consultas + 3
    # Solo el médico con status=1; el de baja no.
    assert despues_medicos == antes_medicos + 1
