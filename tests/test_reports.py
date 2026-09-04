"""Pruebas de los reportes de médicos y pacientes (`GET /reports/*`).

Dos cosas que estas pruebas defienden por encima del resto:

1. **La exportación trae lo mismo que la vista previa.** Es la promesa del feature ("exportas
   lo que estás viendo") y la única forma de romperla en silencio es que los filtros se
   apliquen en dos sitios distintos. Hay un test que compara el `total` de la vista previa con
   las filas del Excel para el mismo filtro.
2. **Solo `super_admin`.** Un `admin` NO puede exportar; es la razón por la que el permiso se
   siembra mapeado a un único rol, así que hay test de endpoint y test de la migración.

La BD local tiene datos de producción restaurados (miles de filas ya committeadas), así que
nada se asierta en absoluto: se siembran filas conocidas y se buscan por su propio filtro
(`search` con un marcador único) o se comparan deltas.
"""

import io
import re
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.tz import VET, day_bounds, to_local
from src.models.audit_log import AuditLog
from src.models.consultation import Consultation
from src.models.doctor import Doctor
from src.models.patient import Patient
from src.models.professional_type import ProfessionalType
from src.models.rbac import Permission, Role, RolePermission
from src.models.specialty import Specialty
from src.services import reports as reports_service
from tests._helpers import any_specialty_id, auth_headers, make_profile

PREFIX = "/api/v1"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
REPORTS_MIGRATION = "20260903_093414_seed_reports_export_permission.sql"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- Utilidades ---------------------------------------------------------------


def _statements(sql: str) -> list[str]:
    """Parte una migración en statements. Los comentarios `--` se quitan ANTES de partir por
    ';' porque el propio comentario puede contener uno (mismo helper que test_stats)."""
    return [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]


async def _apply_reports_migration(db: AsyncSession) -> None:
    """Siembra `reports.export` en la sesión del test.

    Hace falta porque la BD local puede estar restaurada de un backup anterior a esta
    migración, y sin el permiso el `super_admin` recibiría 403 y todos los tests darían un
    falso rojo (o peor: un falso verde en los de autorización).
    """
    for statement in _statements((MIGRATIONS_DIR / REPORTS_MIGRATION).read_text(encoding="utf-8")):
        await db.execute(text(statement))


@pytest.fixture
async def super_admin(db_session: AsyncSession):
    """Un `super_admin` con el permiso `reports.export` ya sembrado."""
    await _apply_reports_migration(db_session)
    profile = make_profile(role="super_admin")
    db_session.add(profile)
    await db_session.flush()
    return profile


def _sheet_rows(content: bytes, sheet: str = "sheet2.xml") -> str:
    """El XML de una hoja del .xlsx. Un `.xlsx` es un ZIP de XMLs, así que se puede inspeccionar
    con la stdlib (`zipfile`) sin añadir una dependencia de lectura de Excel solo para los tests.

    `sheet2.xml` es la hoja de datos: la primera del libro es la portada con los filtros.
    """
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        return book.read(f"xl/worksheets/{sheet}").decode("utf-8")


def _shared_strings(content: bytes) -> str:
    """Todas las cadenas del libro. xlsxwriter guarda el texto en la tabla de cadenas
    compartidas y las celdas solo apuntan a su índice, así que buscar un nombre en el XML de la
    hoja no lo encuentra: hay que mirar aquí."""
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        return book.read("xl/sharedStrings.xml").decode("utf-8")


async def _seed_doctor(db: AsyncSession, marker: str, **overrides) -> Doctor:
    """Un médico con ficha habilitada (verificada, con cédula y licencia) y cuenta ligada."""
    profile = make_profile(role="doctor")
    profile.full_name = f"Doctor {marker}"
    db.add(profile)
    await db.flush()
    fields = {
        "full_name": f"Doctor {marker}",
        "cedula": f"V-{uuid.uuid4().int % 10**8:08d}",
        "license": "MPPS-99999",
        "email": f"{marker}@example.com",
        "phone": "+584120000000",
        "country_of_residence": "Venezuela",
        "status": 1,
        "verified": True,
    }
    fields.update(overrides)
    doctor = Doctor(user_id=profile.id, **fields)
    db.add(doctor)
    await db.flush()
    return doctor


async def _seed_patient(db: AsyncSession, marker: str, **overrides) -> Patient:
    fields = {
        "full_name": f"Paciente {marker}",
        "phone_whatsapp": "+584121111111",
        "affected_zone": "Caracas",
        "consent": True,
        "cedula": f"V-{uuid.uuid4().int % 10**8:08d}",
        "age_range": "30-39",
        "email": f"{marker}@example.com",
        "allergies": "Penicilina",
        "needs_tags": ["Fiebre"],
        "description": "Caso de prueba",
    }
    fields.update(overrides)
    patient = Patient(**fields)
    db.add(patient)
    await db.flush()
    return patient


# --- Autorización: solo super_admin -------------------------------------------


async def test_doctors_preview_ok_para_super_admin(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    resp = await client.get(
        f"{PREFIX}/reports/doctors", headers=auth_headers(super_admin.id), params={"limit": 1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {"columns", "rows", "total", "filters"} == set(body)
    assert [c["key"] for c in body["columns"]] == [c.key for c in reports_service.DOCTOR_COLUMNS]


async def test_admin_no_puede_exportar(client: AsyncClient, db_session: AsyncSession) -> None:
    """El permiso se siembra SOLO para super_admin: un admin (que sí tiene `stats.read` y todo
    lo operativo) no exporta PII masiva. Si este test se vuelve verde con 200, la migración
    mapeó el permiso a `admin` y el feature dejó de cumplir su requisito."""
    await _apply_reports_migration(db_session)
    admin = make_profile(role="admin")
    db_session.add(admin)
    await db_session.flush()
    for path in ("/reports/doctors", "/reports/doctors/export", "/reports/patients"):
        resp = await client.get(f"{PREFIX}{path}", headers=auth_headers(admin.id))
        assert resp.status_code == 403, f"{path} -> {resp.status_code}"


async def test_patient_y_anonimo_no_acceden(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _apply_reports_migration(db_session)
    patient = make_profile(role="patient")
    db_session.add(patient)
    await db_session.flush()
    assert (
        await client.get(f"{PREFIX}/reports/patients", headers=auth_headers(patient.id))
    ).status_code == 403
    assert (await anon_client.get(f"{PREFIX}/reports/patients")).status_code == 401


# --- Contenido del reporte de médicos -----------------------------------------


async def test_doctor_row_trae_ficha_y_actividad(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """La fila resuelve etiquetas (no códigos crudos) y cuenta las consultas del médico."""
    marker = f"rep{uuid.uuid4().hex[:8]}"
    doctor = await _seed_doctor(db_session, marker)
    patient = await _seed_patient(db_session, marker)
    db_session.add_all(
        [
            Consultation(
                patient_id=patient.id, assigned_doctor_id=doctor.user_id, status="closed"
            ),
            Consultation(
                patient_id=patient.id, assigned_doctor_id=doctor.user_id, status="waiting"
            ),
        ]
    )
    await db_session.flush()

    resp = await client.get(
        f"{PREFIX}/reports/doctors",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    row = body["rows"][0]
    assert row["full_name"] == f"Doctor {marker}"
    assert row["status"] == "Activo"  # no el entero 1
    assert row["verified"] == "Sí"
    assert row["can_practice"] == "Sí"
    assert row["blocked_reason"] == ""
    assert row["has_account"] == "Sí"
    assert row["country"] == "Venezuela"
    assert row["consultations_total"] == 2
    assert row["consultations_closed"] == 1  # `waiting` no cuenta como cerrada
    assert row["last_consultation_at"] is not None


async def test_doctor_bloqueado_reporta_su_motivo(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """El reporte usa el MISMO criterio de habilitación que el gate de acceso de los médicos:
    una ficha sin licencia no atiende, aunque esté marcada `verified`."""
    marker = f"rep{uuid.uuid4().hex[:8]}"
    await _seed_doctor(db_session, marker, license=None)

    resp = await client.get(
        f"{PREFIX}/reports/doctors",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    row = resp.json()["rows"][0]
    assert row["can_practice"] == "No"
    assert row["blocked_reason"] == "Sin licencia"


async def test_filtro_can_practice_excluye_al_bloqueado(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = f"rep{uuid.uuid4().hex[:8]}"
    await _seed_doctor(db_session, marker, license=None)

    habilitados = await client.get(
        f"{PREFIX}/reports/doctors",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "can_practice": "true"},
    )
    bloqueados = await client.get(
        f"{PREFIX}/reports/doctors",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "can_practice": "false"},
    )
    assert habilitados.json()["total"] == 0
    assert bloqueados.json()["total"] == 1


async def test_filtro_por_rango_de_fechas_incluye_el_dia_final(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """`created_to` es INCLUSIVO. El bug clásico de estos filtros es comparar contra la
    medianoche del día indicado, que excluye ese día entero — justo el que se acaba de pedir."""
    marker = f"rep{uuid.uuid4().hex[:8]}"
    doctor = await _seed_doctor(db_session, marker)
    hoy_vzla = to_local(doctor.created_at).date()

    resp = await client.get(
        f"{PREFIX}/reports/doctors",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "created_from": str(hoy_vzla), "created_to": str(hoy_vzla)},
    )
    assert resp.json()["total"] == 1

    fuera = await client.get(
        f"{PREFIX}/reports/doctors",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "created_from": str(hoy_vzla + timedelta(days=1))},
    )
    assert fuera.json()["total"] == 0


# --- Contenido del reporte de pacientes ---------------------------------------


async def test_patient_row_trae_ficha_origen_y_ultimo_caso(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = f"rep{uuid.uuid4().hex[:8]}"
    patient = await _seed_patient(db_session, marker)
    db_session.add_all(
        [
            Consultation(
                patient_id=patient.id,
                status="closed",
                created_at=datetime.now(UTC) - timedelta(days=2),
            ),
            Consultation(patient_id=patient.id, status="waiting"),
        ]
    )
    await db_session.flush()

    resp = await client.get(
        f"{PREFIX}/reports/patients",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()["rows"][0]
    assert row["full_name"] == f"Paciente {marker}"
    assert row["origin"] == "Cola pública"
    assert row["allergies"] == "Penicilina"
    assert row["needs_tags"] == "Fiebre"
    assert row["consent"] == "Sí"
    assert row["consultations_total"] == 2
    assert row["consultations_closed"] == 1
    # El estado del ÚLTIMO caso, en español: es lo que dice si el paciente sigue esperando.
    assert row["last_consultation_status"] == "Esperando"
    assert row["archived"] == "No"


async def test_filtro_origen_separa_cola_publica_de_consultorio(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = f"rep{uuid.uuid4().hex[:8]}"
    medico = make_profile(role="doctor")
    medico.full_name = f"Dra Consultorio {marker}"
    db_session.add(medico)
    await db_session.flush()
    await _seed_patient(db_session, marker)  # cola pública
    await _seed_patient(db_session, f"{marker}b", created_by_doctor_id=medico.id)
    # Ambos comparten el prefijo del marcador, así que un `search` los trae a los dos.
    todos = await client.get(
        f"{PREFIX}/reports/patients",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert todos.json()["total"] == 2

    consultorio = await client.get(
        f"{PREFIX}/reports/patients",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "origin": "consultorio"},
    )
    body = consultorio.json()
    assert body["total"] == 1
    assert body["rows"][0]["origin"] == "Consultorio"
    assert body["rows"][0]["registered_by"] == f"Dra Consultorio {marker}"


async def test_filtro_has_consultations_encuentra_a_los_que_nunca_tuvieron_caso(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = f"rep{uuid.uuid4().hex[:8]}"
    con_caso = await _seed_patient(db_session, marker)
    await _seed_patient(db_session, f"{marker}b")
    db_session.add(Consultation(patient_id=con_caso.id, status="waiting"))
    await db_session.flush()

    sin_caso = await client.get(
        f"{PREFIX}/reports/patients",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "has_consultations": "false"},
    )
    body = sin_caso.json()
    assert body["total"] == 1
    assert body["rows"][0]["consultations_total"] == 0


async def test_archivados_fuera_salvo_que_se_pidan(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """Un informe que cuenta pacientes archivados sin decirlo infla los totales."""
    marker = f"rep{uuid.uuid4().hex[:8]}"
    await _seed_patient(db_session, marker, deleted_at=datetime.now(UTC))

    por_defecto = await client.get(
        f"{PREFIX}/reports/patients",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert por_defecto.json()["total"] == 0

    incluidos = await client.get(
        f"{PREFIX}/reports/patients",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "include_archived": "true"},
    )
    body = incluidos.json()
    assert body["total"] == 1
    assert body["rows"][0]["archived"] == "Sí"


async def test_filtro_por_necesidad_usa_el_array_completo(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = f"rep{uuid.uuid4().hex[:8]}"
    await _seed_patient(db_session, marker, needs_tags=["Apoyo emocional", "Fiebre"])

    resp = await client.get(
        f"{PREFIX}/reports/patients",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "need_tag": "Apoyo emocional"},
    )
    assert resp.json()["total"] == 1
    vacio = await client.get(
        f"{PREFIX}/reports/patients",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "need_tag": "Traumatología"},
    )
    assert vacio.json()["total"] == 0


async def test_filtros_de_catalogo_acotan_y_se_nombran_en_la_portada(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """Especialidad y tipo profesional filtran por id, pero la portada del Excel debe decir el
    NOMBRE: un informe cuyos filtros son UUIDs no se puede auditar sin abrir la base."""
    marker = f"rep{uuid.uuid4().hex[:8]}"
    specialty_id = await any_specialty_id(client)
    ptype_id = str(
        await db_session.scalar(
            select(ProfessionalType.id).where(ProfessionalType.deleted_at.is_(None)).limit(1)
        )
    )
    await _seed_doctor(
        db_session,
        marker,
        specialty_id=uuid.UUID(specialty_id),
        professional_type_id=uuid.UUID(ptype_id),
    )

    dentro = await client.get(
        f"{PREFIX}/reports/doctors",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "specialty_id": specialty_id, "professional_type_id": ptype_id},
    )
    assert dentro.json()["total"] == 1

    fuera = await client.get(
        f"{PREFIX}/reports/doctors",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "specialty_id": str(uuid.uuid4())},
    )
    assert fuera.json()["total"] == 0

    export = await client.get(
        f"{PREFIX}/reports/doctors/export",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "specialty_id": specialty_id, "professional_type_id": ptype_id},
    )
    strings = _shared_strings(export.content)
    nombre_especialidad = await db_session.scalar(
        select(Specialty.name).where(Specialty.id == uuid.UUID(specialty_id))
    )
    assert nombre_especialidad in strings
    assert specialty_id not in strings, "la portada escribió el UUID en vez del nombre"

    # Y la VISTA PREVIA declara los mismos filtros que la portada. Al resolver los nombres de
    # catálogo solo en la exportación, la previa se saltaba estos dos chips: el total bajaba por
    # un filtro que el usuario no veía listado en ninguna parte.
    etiquetas = dict(tuple(f) for f in dentro.json()["filters"])
    assert etiquetas["Especialidad"] == nombre_especialidad
    nombre_tipo = await db_session.scalar(
        select(ProfessionalType.name).where(ProfessionalType.id == uuid.UUID(ptype_id))
    )
    assert etiquetas["Tipo profesional"] == nombre_tipo


async def test_filtros_de_ficha_del_paciente(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """Zona, edad, cuenta, consentimiento y origen público: los filtros de la ficha, cada uno
    acotando de verdad (no solo aceptados y descartados)."""
    marker = f"rep{uuid.uuid4().hex[:8]}"
    cuenta = make_profile(role="patient")
    db_session.add(cuenta)
    await db_session.flush()
    await _seed_patient(
        db_session,
        marker,
        affected_zone="Mérida",
        age_range="60-69",
        user_id=cuenta.id,
        consent=True,
    )
    await _seed_patient(db_session, f"{marker}b", affected_zone="Zulia", age_range="18-29")

    async def total(**params) -> int:
        resp = await client.get(
            f"{PREFIX}/reports/patients",
            headers=auth_headers(super_admin.id),
            params={"search": marker, **params},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["total"]

    assert await total() == 2
    assert await total(origin="publica") == 2  # ninguno es de consultorio
    assert await total(affected_zone="Mérida") == 1
    assert await total(age_range="60-69") == 1
    assert await total(has_account="true") == 1
    assert await total(has_account="false") == 1
    assert await total(consent="true") == 2
    assert await total(consent="false") == 0
    hoy = to_local(datetime.now(UTC)).date()
    assert await total(created_from=str(hoy), created_to=str(hoy)) == 2
    assert await total(created_from=str(hoy + timedelta(days=1))) == 0


async def test_la_vista_previa_pagina_con_skip(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """La segunda página trae filas distintas de la primera y el `total` no cambia: el total es
    del reporte completo, no de la página."""
    marker = f"rep{uuid.uuid4().hex[:8]}"
    for i in range(3):
        await _seed_patient(db_session, f"{marker}{i}")

    async def page(skip: int) -> dict:
        resp = await client.get(
            f"{PREFIX}/reports/patients",
            headers=auth_headers(super_admin.id),
            params={"search": marker, "skip": skip, "limit": 2},
        )
        return resp.json()

    primera, segunda = await page(0), await page(2)
    assert primera["total"] == segunda["total"] == 3
    assert len(primera["rows"]) == 2
    assert len(segunda["rows"]) == 1
    ids = {r["patient_id"] for r in primera["rows"]} | {r["patient_id"] for r in segunda["rows"]}
    assert len(ids) == 3, "la paginación repitió filas entre páginas"


# --- Exportación a Excel ------------------------------------------------------


async def test_export_devuelve_un_xlsx_valido_con_portada_y_datos(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = f"rep{uuid.uuid4().hex[:8]}"
    await _seed_doctor(db_session, marker)

    resp = await client.get(
        f"{PREFIX}/reports/doctors/export",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == XLSX_MEDIA_TYPE
    hoy = f"{to_local(datetime.now(UTC)):%Y-%m-%d}"
    assert resp.headers["content-disposition"] == f'attachment; filename="medicos-{hoy}.xlsx"'

    content = resp.content
    assert content[:2] == b"PK", "no es un contenedor ZIP: el .xlsx está corrupto"
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        names = book.namelist()
    assert "xl/worksheets/sheet1.xml" in names  # portada
    assert "xl/worksheets/sheet2.xml" in names  # datos

    strings = _shared_strings(content)
    assert "Reporte de médicos" in strings
    assert "Filtros aplicados" in strings
    assert f"Doctor {marker}" in strings
    # La portada firma quién exportó y con qué filtro: sin eso el archivo es un volcado anónimo.
    assert marker in strings  # el valor del filtro `search`
    assert "Nombre completo" in strings  # cabecera de la hoja de datos


async def test_export_trae_exactamente_las_filas_de_la_vista_previa(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """La promesa del feature: mismo filtro -> mismas filas. Se comprueba contando las filas
    reales de la hoja de datos del Excel y comparándolas con el `total` de la vista previa."""
    marker = f"rep{uuid.uuid4().hex[:8]}"
    for i in range(3):
        await _seed_patient(db_session, f"{marker}{i}")

    preview = await client.get(
        f"{PREFIX}/reports/patients",
        headers=auth_headers(super_admin.id),
        params={"search": marker, "limit": 1},  # la vista previa pagina; el export no
    )
    body = preview.json()
    assert body["total"] == 3
    assert len(body["rows"]) == 1

    export = await client.get(
        f"{PREFIX}/reports/patients/export",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    sheet = _sheet_rows(export.content)
    # `<row r="1">` es la cabecera; las filas de datos van de la 2 en adelante.
    filas_datos = len(re.findall(r'<row r="(?!1")', sheet))
    assert filas_datos == body["total"] == 3


async def test_export_registra_la_extraccion_en_audit_log(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """Una extracción masiva de PII que no deja rastro es indistinguible de que no ocurriera."""
    marker = f"rep{uuid.uuid4().hex[:8]}"
    await _seed_patient(db_session, marker)

    resp = await client.get(
        f"{PREFIX}/reports/patients/export",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert resp.status_code == 200, resp.text

    entry = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "report.exported", AuditLog.actor_user_id == super_admin.id)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.resource == "reports"
    assert entry.resource_id == "patients"
    assert entry.metadata_["rows"] == 1
    # El filtro queda registrado; las FILAS no (el audit no debe contener la PII que audita).
    assert marker in str(entry.metadata_["filters"])
    assert "Penicilina" not in str(entry.metadata_)


async def test_export_rechaza_un_filtro_demasiado_amplio(
    client: AsyncClient, super_admin, db_session: AsyncSession, monkeypatch
) -> None:
    """Con el tope a 0 filas, cualquier export debe caer con 422 y un mensaje que diga qué
    hacer — no con un 500 ni con un worker sin memoria."""
    await _seed_patient(db_session, f"rep{uuid.uuid4().hex[:8]}")
    monkeypatch.setattr(reports_service, "MAX_EXPORT_ROWS", 0)

    resp = await client.get(
        f"{PREFIX}/reports/patients/export", headers=auth_headers(super_admin.id)
    )
    assert resp.status_code == 422, resp.text
    assert "Acota el reporte" in resp.json()["detail"]


async def test_export_sin_resultados_produce_un_libro_abrible(
    client: AsyncClient, super_admin
) -> None:
    """Cero filas es un resultado válido, no un error: el archivo debe seguir siendo un .xlsx
    íntegro (el autofiltro sobre un rango vacío es lo que Excel marca como corrupto)."""
    resp = await client.get(
        f"{PREFIX}/reports/doctors/export",
        headers=auth_headers(super_admin.id),
        params={"search": f"inexistente-{uuid.uuid4().hex}"},
    )
    assert resp.status_code == 200, resp.text
    sheet = _sheet_rows(resp.content)
    assert len(re.findall(r'<row r="(?!1")', sheet)) == 0
    with zipfile.ZipFile(io.BytesIO(resp.content)) as book:
        assert book.testzip() is None


# --- Hora de Venezuela --------------------------------------------------------


# --- Reporte de consultas (el del monitor del dashboard) ----------------------


async def _seed_consultation(
    db: AsyncSession, marker: str, *, patient: Patient, **overrides
) -> Consultation:
    """Una consulta con paciente, codigo unico y estado `in_progress` por defecto."""
    fields = {
        "code": f"T-{marker}",
        "status": "in_progress",
        "priority": "normal",
        "chief_complaint": f"Motivo {marker}",
        "queued_at": datetime.now(UTC) - timedelta(hours=5),
        "opened_at": datetime.now(UTC) - timedelta(hours=4),
    }
    fields.update(overrides)
    consultation = Consultation(patient_id=patient.id, **fields)
    db.add(consultation)
    await db.flush()
    return consultation


async def _consultation_rows(client: AsyncClient, super_admin, **params):
    resp = await client.get(
        f"{PREFIX}/reports/consultations", headers=auth_headers(super_admin.id), params=params
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["rows"], body["total"]


async def test_consultas_columnas_empiezan_por_las_del_monitor(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """Las primeras columnas son, EN ORDEN, las del modal del dashboard. Es lo que hace que el
    informe sea reconocible como "esa tabla"; si alguien reordena, esto avisa."""
    resp = await client.get(
        f"{PREFIX}/reports/consultations",
        headers=auth_headers(super_admin.id),
        params={"limit": 1},
    )
    assert resp.status_code == 200, resp.text
    cabeceras = [c["header"] for c in resp.json()["columns"]]
    assert cabeceras[:4] == ["Estado", "Médico asignado", "Paciente", "Tiempo en progreso"]
    assert cabeceras[4] == "Horas en progreso"
    assert cabeceras[5] == "Motivo de consulta"


async def test_consulta_row_trae_paciente_medico_y_tiempo(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:10]
    doctor_profile = make_profile(role="doctor")
    doctor_profile.full_name = f"Dra {marker}"
    db_session.add(doctor_profile)
    await db_session.flush()
    patient = await _seed_patient(db_session, marker)
    await _seed_consultation(
        db_session,
        marker,
        patient=patient,
        assigned_doctor_id=doctor_profile.id,
        opened_at=datetime.now(UTC) - timedelta(hours=4),
    )

    rows, total = await _consultation_rows(client, super_admin, search=marker)
    assert total == 1
    row = rows[0]
    assert row["status"] == "Abierta"
    assert row["doctor"] == f"Dra {marker}"
    assert row["patient"] == f"Paciente {marker}"
    assert row["elapsed"] == "4 horas"  # mismo texto que pinta el modal
    assert row["elapsed_hours"] == pytest.approx(4.0, abs=0.2)
    assert row["chief_complaint"] == f"Motivo {marker}"
    assert row["patient_phone"] == "+584121111111"


async def test_consulta_sin_medico_dice_sin_asignar(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """Mismo texto que la pantalla: quien compare el Excel con el panel no deberia tener que
    traducir un hueco."""
    marker = uuid.uuid4().hex[:10]
    patient = await _seed_patient(db_session, marker)
    await _seed_consultation(db_session, marker, patient=patient, assigned_doctor_id=None)
    rows, _ = await _consultation_rows(client, super_admin, search=marker)
    assert rows[0]["doctor"] == "— sin asignar —"


async def test_filtro_de_estados_acota_al_set_del_monitor(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """El set del monitor incluye canceladas y no-show, y deja fuera las que esperan y las
    cerradas. Sin esto, el informe "en progreso" traeria la cola entera."""
    marker = uuid.uuid4().hex[:10]
    patient = await _seed_patient(db_session, marker)
    await _seed_consultation(db_session, f"{marker}A", patient=patient, status="in_progress")
    await _seed_consultation(db_session, f"{marker}B", patient=patient, status="cancelled")
    await _seed_consultation(db_session, f"{marker}C", patient=patient, status="waiting")
    await _seed_consultation(db_session, f"{marker}D", patient=patient, status="closed")

    _, total = await _consultation_rows(
        client, super_admin, search=marker, status=list(reports_service.IN_PROGRESS_STATUSES)
    )
    assert total == 2  # in_progress + cancelled; fuera waiting y closed

    _, todos = await _consultation_rows(client, super_admin, search=marker)
    assert todos == 4  # sin filtro de estado entran los cuatro


async def test_estado_inventado_da_422_y_no_un_vacio_silencioso(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """Un estado mal escrito devolveria cero filas, y quien lo pidio leeria ese vacio como "no
    hay casos" en vez de como "escribiste mal el filtro"."""
    resp = await client.get(
        f"{PREFIX}/reports/consultations",
        headers=auth_headers(super_admin.id),
        params={"status": ["in_progress", "en_progreso"]},
    )
    assert resp.status_code == 422, resp.text
    assert "en_progreso" in resp.json()["detail"]


async def test_filtro_por_medico_y_por_sin_asignar(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:10]
    doctor_profile = make_profile(role="doctor")
    db_session.add(doctor_profile)
    await db_session.flush()
    patient = await _seed_patient(db_session, marker)
    await _seed_consultation(
        db_session, f"{marker}A", patient=patient, assigned_doctor_id=doctor_profile.id
    )
    await _seed_consultation(db_session, f"{marker}B", patient=patient, assigned_doctor_id=None)

    _, asignadas = await _consultation_rows(
        client, super_admin, search=marker, assigned_doctor_id=str(doctor_profile.id)
    )
    assert asignadas == 1
    _, huerfanas = await _consultation_rows(client, super_admin, search=marker, unassigned="true")
    assert huerfanas == 1


async def test_tiempo_de_una_consulta_cerrada_se_mide_hasta_el_cierre(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """Medir siempre contra "ahora" daria, para un caso cerrado hace meses, un tiempo enorme que
    no significa nada. Para las del monitor (que no han cerrado) el resultado no cambia."""
    marker = uuid.uuid4().hex[:10]
    patient = await _seed_patient(db_session, marker)
    ahora = datetime.now(UTC)
    await _seed_consultation(
        db_session,
        marker,
        patient=patient,
        status="closed",
        opened_at=ahora - timedelta(days=30),
        closed_at=ahora - timedelta(days=30) + timedelta(hours=2),
    )
    rows, _ = await _consultation_rows(client, super_admin, search=marker)
    assert rows[0]["elapsed"] == "2 horas"
    assert rows[0]["elapsed_hours"] == pytest.approx(2.0, abs=0.1)


async def test_export_de_consultas_es_un_xlsx_con_los_datos(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:10]
    patient = await _seed_patient(db_session, marker)
    await _seed_consultation(db_session, marker, patient=patient)

    resp = await client.get(
        f"{PREFIX}/reports/consultations/export",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == XLSX_MEDIA_TYPE
    assert "consultas-" in resp.headers["content-disposition"]

    cadenas = _shared_strings(resp.content)
    assert f"Paciente {marker}" in cadenas
    assert f"Motivo {marker}" in cadenas
    assert "Tiempo en progreso" in cadenas


async def test_export_de_consultas_trae_lo_mismo_que_la_vista_previa(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """La promesa del feature: exportas lo que estas viendo."""
    marker = uuid.uuid4().hex[:10]
    patient = await _seed_patient(db_session, marker)
    for i in range(3):
        await _seed_consultation(db_session, f"{marker}{i}", patient=patient)

    _, total = await _consultation_rows(client, super_admin, search=marker)
    assert total == 3

    resp = await client.get(
        f"{PREFIX}/reports/consultations/export",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    filas = _sheet_rows(resp.content).count("<row ")
    assert filas == total + 1  # + la cabecera


async def test_export_de_consultas_queda_en_audit_log(
    client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = uuid.uuid4().hex[:10]
    patient = await _seed_patient(db_session, marker)
    await _seed_consultation(db_session, marker, patient=patient)

    resp = await client.get(
        f"{PREFIX}/reports/consultations/export",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert resp.status_code == 200, resp.text
    entrada = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "report.exported", AuditLog.resource_id == "consultations")
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entrada.actor_user_id == super_admin.id
    # El filtro si; las filas NO: el audit no debe contener la PII que audita.
    assert marker in str(entrada.metadata_["filters"])
    assert "Motivo" not in str(entrada.metadata_)


def test_etiqueta_de_tiempo_replica_la_del_frontend() -> None:
    """Mismo formato que `lib/utils.ts::tiempoTranscurrido`: minutos, horas, y dias + horas."""
    etiqueta = reports_service._elapsed_label
    assert etiqueta(timedelta(minutes=4)) == "4 min"
    assert etiqueta(timedelta(hours=1)) == "1 hora"
    assert etiqueta(timedelta(hours=7)) == "7 horas"
    assert etiqueta(timedelta(days=1)) == "1 día"
    assert etiqueta(timedelta(days=2)) == "2 días"
    assert etiqueta(timedelta(days=2, hours=3)) == "2 días 3 horas"
    assert etiqueta(None) == ""


def test_day_bounds_cubre_el_dia_completo_en_hora_local() -> None:
    """Un día venezolano va de 04:00 UTC a 04:00 UTC del siguiente. Si estos límites se
    calcularan en UTC, el filtro perdería las primeras/últimas 4 horas de cada día."""
    start, end = day_bounds(date(2026, 9, 3), date(2026, 9, 3))
    assert start == datetime(2026, 9, 3, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
    assert (end - start) == timedelta(days=1)


def test_day_bounds_admite_extremos_abiertos() -> None:
    assert day_bounds(None, None) == (None, None)
    assert day_bounds(date(2026, 1, 1), None)[1] is None
    assert day_bounds(None, date(2026, 1, 1))[0] is None


def test_to_local_convierte_a_utc_menos_4_sin_tzinfo() -> None:
    """Sin tzinfo a propósito: xlsxwriter rechaza datetimes con zona."""
    local = to_local(datetime(2026, 9, 3, 2, 30, tzinfo=UTC))
    assert local == datetime(2026, 9, 2, 22, 30)
    assert local.tzinfo is None
    assert VET.utcoffset(None) == timedelta(hours=-4)


def test_to_local_asume_utc_si_el_dato_viene_sin_zona() -> None:
    """Las columnas son `timestamptz`, pero un naive suelto no debe reinterpretarse como local:
    eso desplazaría la hora 4 horas en silencio."""
    assert to_local(datetime(2026, 9, 3, 12, 0)) == datetime(2026, 9, 3, 8, 0)
    assert to_local(None) is None


# --- Migración del permiso ----------------------------------------------------


async def test_migracion_es_idempotente_y_solo_otorga_a_super_admin(
    db_session: AsyncSession,
) -> None:
    await _apply_reports_migration(db_session)
    await _apply_reports_migration(db_session)  # re-aplicar: no-op

    permisos = (
        (await db_session.execute(select(Permission).where(Permission.code == "reports.export")))
        .scalars()
        .all()
    )
    assert len(permisos) == 1, "la migración duplicó el permiso al re-aplicarse"

    roles = (
        (
            await db_session.execute(
                select(Role.code)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(Permission.code == "reports.export")
            )
        )
        .scalars()
        .all()
    )
    assert sorted(roles) == ["super_admin"], "reports.export debe ser exclusivo de super_admin"


async def test_la_portada_dice_cuando_no_hay_filtros(
    client: AsyncClient, super_admin, db_session: AsyncSession, monkeypatch
) -> None:
    """Un export sin filtros lo declara en la portada. Si la portada quedara en blanco, un
    volcado completo sería indistinguible de un reporte acotado."""
    # Tope alto: la base local tiene datos de producción restaurados y sin filtro son miles.
    monkeypatch.setattr(reports_service, "MAX_EXPORT_ROWS", 200_000)
    resp = await client.get(
        f"{PREFIX}/reports/doctors/export", headers=auth_headers(super_admin.id)
    )
    assert resp.status_code == 200, resp.text
    assert "Sin filtros: todos los registros" in _shared_strings(resp.content)
