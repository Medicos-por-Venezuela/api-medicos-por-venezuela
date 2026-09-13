"""Pruebas de las encuestas de marketing (`/marketing/surveys/*`).

Tres cosas que estas pruebas defienden por encima del resto:

1. **Una respuesta por persona y encuesta.** Responder otra vez reemplaza la fila, también cuando
   llegan dos envíos a la vez; el mismo correo en OTRA encuesta es otra fila.
2. **El backend exige lo que exige el formulario y descarta lo que no preguntó.** El endpoint es
   público: el formulario no es la única puerta, así que la disponibilidad o la zona horaria de un
   psicólogo no pueden depender de que el JavaScript haya validado.
3. **Leer y exportar es solo de super_admin** (`marketing.read`), y el Excel escribe como TEXTO lo
   que tecleó un tercero aunque parezca una fórmula.

Cada test usa un correo único (`<marker>@example.com`) y busca por él: la base local es un restore
de producción y nada se asierta en absoluto.
"""

import asyncio
import io
import re
import uuid
import zipfile
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.tz import to_local
from src.db.session import AsyncSessionLocal
from src.models.audit_log import AuditLog
from src.models.marketing_survey_response import MarketingSurveyResponse
from src.models.rbac import Permission, Role, RolePermission
from src.services import marketing as marketing_service
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"
SURVEYS = f"{PREFIX}/marketing/surveys"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
PERMISSION_MIGRATION = "20260912_191154_seed_marketing_read_permission.sql"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- Utilidades ---------------------------------------------------------------


def _statements(sql: str) -> list[str]:
    """Parte una migración en statements (mismo helper que test_reports)."""
    return [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]


async def _apply_permission_migration(db: AsyncSession) -> None:
    """Siembra `marketing.read` en la sesión del test, por si la base local es un backup anterior
    a la migración: sin el permiso el super_admin recibiría 403 y los tests de autorización
    darían un falso verde."""
    sql = (MIGRATIONS_DIR / PERMISSION_MIGRATION).read_text(encoding="utf-8")
    for statement in _statements(sql):
        await db.execute(text(statement))


@pytest.fixture
async def super_admin(db_session: AsyncSession):
    await _apply_permission_migration(db_session)
    profile = make_profile(role="super_admin")
    db_session.add(profile)
    await db_session.flush()
    return profile


def _marker() -> str:
    return f"mkt{uuid.uuid4().hex[:10]}"


def _answers(email: str, **overrides) -> dict:
    """Respuesta completa y válida para psicólogos/especialistas (disponibilidad y zona)."""
    body = {
        "email": email,
        "roles": ["atender_pacientes"],
        "moments": ["noche"],
        "days": ["martes", "jueves"],
        "weekly_hours": "entre_1_y_3",
        "timezone": "venezuela",
    }
    body.update(overrides)
    return body


async def _stored(db: AsyncSession, survey: str, email: str) -> list[MarketingSurveyResponse]:
    return list(
        (
            await db.execute(
                select(MarketingSurveyResponse).where(
                    MarketingSurveyResponse.survey == survey,
                    MarketingSurveyResponse.email == email,
                )
            )
        )
        .scalars()
        .all()
    )


def _sheet_xml(content: bytes) -> str:
    """XML de la hoja de datos (`sheet2.xml`; la primera es la portada)."""
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        return book.read("xl/worksheets/sheet2.xml").decode("utf-8")


def _shared_strings(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        return book.read("xl/sharedStrings.xml").decode("utf-8")


# --- Envío público ------------------------------------------------------------


async def test_psicologo_responde_sin_sesion_y_queda_normalizado(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Sin cuenta ni token (llega desde el correo masivo). El correo se guarda en minúsculas y las
    opciones en el orden del formulario, sin repetir, lleguen como lleguen."""
    marker = _marker()
    resp = await anon_client.post(
        f"{SURVEYS}/psicologos/responses",
        json=_answers(
            f"Ana.{marker}@Example.COM",
            days=["viernes", "lunes", "viernes"],
            moments=["variable", "manana"],
            availability_notes="  Martes en la noche  ",
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["survey"] == "psicologos"
    # El acuse no devuelve lo respondido (el correo no está verificado).
    assert set(body) == {"survey", "created_at", "updated_at"}

    [row] = await _stored(db_session, "psicologos", f"ana.{marker}@example.com")
    assert row.days == ["lunes", "viernes"]
    assert row.moments == ["manana", "variable"]
    assert row.availability_notes == "Martes en la noche"
    assert row.timezone == "venezuela"


async def test_responder_otra_vez_reemplaza_la_respuesta(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """La encuesta promete que "si más adelante tu situación cambia, lo puedes ajustar": la
    segunda respuesta reemplaza a la primera en vez de sumar otra fila, y lo que ya no marcó
    desaparece."""
    email = f"{_marker()}@example.com"
    primera = await anon_client.post(
        f"{SURVEYS}/especialistas/responses",
        json=_answers(
            email, roles=["atender_pacientes", "otra"], role_other_detail="Charlas", notes="Hola"
        ),
    )
    assert primera.status_code == 201, primera.text
    [antes] = await _stored(db_session, "especialistas", email)
    creada = antes.created_at

    segunda = await anon_client.post(
        f"{SURVEYS}/especialistas/responses",
        json=_answers(email, roles=["coordinar_especialidad"], weekly_hours="mas_de_6"),
    )
    assert segunda.status_code == 201, segunda.text

    [fila] = await _stored(db_session, "especialistas", email)
    assert fila.id == antes.id
    assert fila.roles == ["coordinar_especialidad"]
    assert fila.weekly_hours == "mas_de_6"
    assert fila.role_other_detail is None  # ya no marcó "Otra"
    assert fila.notes is None  # y esta vez no escribió nada
    assert fila.created_at == creada  # la primera respuesta conserva su fecha


async def test_mismo_correo_en_otra_encuesta_es_otra_respuesta(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = f"{_marker()}@example.com"
    for survey in ("psicologos", "especialistas"):
        resp = await anon_client.post(f"{SURVEYS}/{survey}/responses", json=_answers(email))
        assert resp.status_code == 201, resp.text
    assert len(await _stored(db_session, "psicologos", email)) == 1
    assert len(await _stored(db_session, "especialistas", email)) == 1


async def test_opcion_de_otra_encuesta_da_422(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`pedir_interconsultas` existe, pero en la encuesta de médicos generales: en la de psicólogos
    es una opción que nadie vio, y guardarla sería inventar una respuesta."""
    email = f"{_marker()}@example.com"
    resp = await anon_client.post(
        f"{SURVEYS}/psicologos/responses", json=_answers(email, roles=["pedir_interconsultas"])
    )
    assert resp.status_code == 422, resp.text
    assert "pedir_interconsultas" in resp.json()["detail"]
    assert await _stored(db_session, "psicologos", email) == []


@pytest.mark.parametrize(
    ("override", "mensaje"),
    [
        ({"moments": []}, "momento del día"),
        ({"days": []}, "días de la semana"),
        ({"weekly_hours": None}, "horas a la semana"),
        ({"timezone": None}, "zona horaria"),
        ({"weekly_hours": "todo_el_dia"}, "Horas a la semana"),
        ({"timezone": "marte"}, "Dónde estás"),
    ],
)
async def test_psicologos_exigen_disponibilidad_y_ubicacion(
    anon_client: AsyncClient, override: dict, mensaje: str
) -> None:
    resp = await anon_client.post(
        f"{SURVEYS}/psicologos/responses",
        json=_answers(f"{_marker()}@example.com", **override),
    )
    assert resp.status_code == 422, resp.text
    assert mensaje in resp.json()["detail"]


async def test_medico_general_que_solo_pide_interconsultas_guarda_su_disponibilidad(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El formulario de médicos generales se enseña completo: quien solo va a pedir interconsultas
    también contesta la disponibilidad, y se guarda. Al principio se descartaba —la sección solo
    aparecía al marcar atender, liderar u "Otra"—, y con el formulario completo eso borraría en
    silencio lo que la persona acaba de escribir."""
    email = f"{_marker()}@example.com"
    resp = await anon_client.post(
        f"{SURVEYS}/medicos-generales/responses",
        json=_answers(
            email, roles=["pedir_interconsultas"], availability_notes="Lunes", notes="Gracias"
        ),
    )
    assert resp.status_code == 201, resp.text

    [fila] = await _stored(db_session, "medicos-generales", email)
    assert fila.roles == ["pedir_interconsultas"]
    assert fila.moments == ["noche"]
    assert fila.days == ["martes", "jueves"]
    assert fila.weekly_hours == "entre_1_y_3"
    assert fila.availability_notes == "Lunes"
    # Esta encuesta no pregunta dónde está: la zona que llegó se descarta.
    assert fila.timezone is None
    assert fila.notes == "Gracias"


@pytest.mark.parametrize(
    "roles", [["pedir_interconsultas"], ["pedir_interconsultas", "atender_pacientes"]]
)
async def test_medico_general_sin_disponibilidad_da_422(
    anon_client: AsyncClient, roles: list[str]
) -> None:
    """La disponibilidad es obligatoria en médicos generales marque lo que marque, igual que en
    las otras dos encuestas."""
    resp = await anon_client.post(
        f"{SURVEYS}/medicos-generales/responses",
        json={"email": f"{_marker()}@example.com", "roles": roles},
    )
    assert resp.status_code == 422, resp.text
    assert "momento del día" in resp.json()["detail"]


async def test_medico_general_sin_zona_horaria_es_valido(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """La encuesta de médicos generales no pregunta la zona: no puede exigirla."""
    email = f"{_marker()}@example.com"
    resp = await anon_client.post(
        f"{SURVEYS}/medicos-generales/responses",
        json=_answers(
            email,
            roles=["rol_activo"],
            role_active_detail="Coordinar guardias",
            timezone=None,
        ),
    )
    assert resp.status_code == 201, resp.text
    [fila] = await _stored(db_session, "medicos-generales", email)
    assert fila.role_active_detail == "Coordinar guardias"
    assert fila.weekly_hours == "entre_1_y_3"


async def test_textos_de_opciones_no_marcadas_se_descartan(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El texto de "Otra" solo se guarda si marcó "Otra", y el de "rol más activo" solo existe en
    médicos generales. La ubicación escrita es distinta: su campo se ve siempre bajo el selector,
    así que se guarda elija lo que elija (precisar "Maracaibo" tras elegir Venezuela es válido)."""
    sin_marcar = f"{_marker()}@example.com"
    resp = await anon_client.post(
        f"{SURVEYS}/psicologos/responses",
        json=_answers(
            sin_marcar,
            role_other_detail="No marcó otra",
            role_active_detail="No existe en esta encuesta",
            timezone_other="Maracaibo",
        ),
    )
    assert resp.status_code == 201, resp.text
    [fila] = await _stored(db_session, "psicologos", sin_marcar)
    assert fila.role_other_detail is None
    assert fila.role_active_detail is None
    assert fila.timezone == "venezuela"
    assert fila.timezone_other == "Maracaibo"

    marcadas = f"{_marker()}@example.com"
    resp = await anon_client.post(
        f"{SURVEYS}/psicologos/responses",
        json=_answers(
            marcadas,
            roles=["otra"],
            role_other_detail="Supervisión clínica",
            timezone="otra",
            timezone_other="Japón (GMT+9)",
            notes="   ",
        ),
    )
    assert resp.status_code == 201, resp.text
    [fila] = await _stored(db_session, "psicologos", marcadas)
    assert fila.role_other_detail == "Supervisión clínica"
    assert fila.timezone_other == "Japón (GMT+9)"
    assert fila.notes is None  # solo espacios cuenta como no respondido


async def test_honeypot_con_valor_rechaza_sin_guardar(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Mismo campo trampa que el registro de médicos: un humano no lo ve, un bot lo rellena."""
    email = f"{_marker()}@example.com"
    resp = await anon_client.post(
        f"{SURVEYS}/psicologos/responses",
        json=_answers(email, website="https://spam.example"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Solicitud inválida."
    assert await _stored(db_session, "psicologos", email) == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("pacientes", {"email": "x@example.com", "roles": ["atender_pacientes"]}),
        ("psicologos", {"email": "no-es-un-correo", "roles": ["atender_pacientes"]}),
        ("psicologos", {"email": "x@example.com", "roles": []}),
        ("psicologos", {"email": "x@example.com", "roles": ["otra"], "admin": True}),
        ("psicologos", {"email": "x@example.com", "roles": ["otra"], "notes": "a" * 2001}),
    ],
)
async def test_envios_malformados_dan_422(anon_client: AsyncClient, path: str, body: dict) -> None:
    """Encuesta inexistente, correo inválido, sin ninguna forma de participar, campo de más
    (mass assignment) y texto desmedido."""
    resp = await anon_client.post(f"{SURVEYS}/{path}/responses", json=body)
    assert resp.status_code == 422, resp.text


# --- Concurrencia -------------------------------------------------------------


@pytest_asyncio.fixture
async def correo_concurrente() -> AsyncGenerator[str, None]:
    """Correo para la prueba con sesiones REALES (committea): se borra al terminar."""
    email = f"{_marker()}@example.com"
    try:
        yield email
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(
                delete(MarketingSurveyResponse).where(MarketingSurveyResponse.email == email)
            )
            await s.commit()


async def test_cinco_envios_simultaneos_del_mismo_correo_dejan_una_sola_fila(
    live_client: AsyncClient, correo_concurrente: str
) -> None:
    """El doble clic en "Enviar", o el mismo correo abierto en dos pestañas. Con un
    read-then-write, dos transacciones verían "no existe" a la vez y una de ellas reventaría con
    la clave única; el upsert deja que la base resuelva y todos reciben 201."""

    async def enviar(hours: str) -> int:
        resp = await live_client.post(
            f"{SURVEYS}/psicologos/responses",
            json=_answers(correo_concurrente, weekly_hours=hours),
        )
        return resp.status_code

    horas = ["menos_de_1", "entre_1_y_3", "entre_3_y_6", "mas_de_6", "entre_1_y_3"]
    codigos = await asyncio.gather(*[enviar(h) for h in horas])
    assert codigos == [201] * 5, codigos

    async with AsyncSessionLocal() as s:
        total = await s.scalar(
            select(func.count()).where(MarketingSurveyResponse.email == correo_concurrente)
        )
    assert total == 1


# --- Listado y exportación: autorización -------------------------------------


async def test_listado_y_export_solo_para_super_admin(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession, super_admin
) -> None:
    """`marketing.read` se siembra SOLO para super_admin. Si un admin empieza a recibir 200, la
    migración lo mapeó a otro rol."""
    admin = make_profile(role="admin")
    patient = make_profile(role="patient")
    db_session.add_all([admin, patient])
    await db_session.flush()

    for path in ("/psicologos/responses", "/medicos-generales/responses/export"):
        url = f"{SURVEYS}{path}"
        assert (await anon_client.get(url)).status_code == 401
        assert (await client.get(url, headers=auth_headers(admin.id))).status_code == 403
        assert (await client.get(url, headers=auth_headers(patient.id))).status_code == 403
        ok = await client.get(url, headers=auth_headers(super_admin.id))
        assert ok.status_code == 200, f"{path} -> {ok.status_code}: {ok.text}"


async def test_encuesta_inexistente_en_el_listado_da_422(client: AsyncClient, super_admin) -> None:
    resp = await client.get(f"{SURVEYS}/pacientes/responses", headers=auth_headers(super_admin.id))
    assert resp.status_code == 422, resp.text


# --- Listado: contenido -------------------------------------------------------


@pytest.mark.parametrize(
    ("survey", "tiene", "no_tiene"),
    [
        ("medicos-generales", {"role_active_detail"}, {"timezone", "timezone_other"}),
        ("psicologos", {"timezone", "timezone_other"}, {"role_active_detail"}),
        ("especialistas", {"timezone", "timezone_other"}, {"role_active_detail"}),
    ],
)
async def test_cada_encuesta_trae_sus_columnas(
    client: AsyncClient, super_admin, survey: str, tiene: set, no_tiene: set
) -> None:
    """Las preguntas que una encuesta no hace no salen como columnas siempre vacías."""
    resp = await client.get(
        f"{SURVEYS}/{survey}/responses",
        headers=auth_headers(super_admin.id),
        params={"limit": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {"columns", "rows", "total", "filters"} == set(body)
    keys = [c["key"] for c in body["columns"]]
    assert keys[:3] == ["email", "updated_at", "roles"]
    assert tiene <= set(keys)
    assert not no_tiene & set(keys)
    expected = marketing_service.survey_columns(marketing_service.SURVEYS[survey])
    assert keys == [c.key for c in expected]


async def test_listado_resuelve_etiquetas_y_solo_trae_su_encuesta(
    client: AsyncClient, anon_client: AsyncClient, super_admin
) -> None:
    """La fila dice lo que vio quien respondió (el texto de SU encuesta), no los códigos, y la
    pestaña de psicólogos no mezcla respuestas de especialistas."""
    marker = _marker()
    email = f"{marker}@example.com"
    for survey in ("psicologos", "especialistas"):
        await anon_client.post(
            f"{SURVEYS}/{survey}/responses",
            json=_answers(email, roles=["atender_pacientes", "otra"], role_other_detail="Grupos"),
        )

    resp = await client.get(
        f"{SURVEYS}/psicologos/responses",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    row = body["rows"][0]
    assert row["email"] == email
    assert row["roles"] == (
        "Atender pacientes a través de la plataforma; Otra forma que quiero proponerles"
    )
    assert row["role_other_detail"] == "Grupos"
    assert row["moments"] == "Noche"
    assert row["days"] == "Martes; Jueves"
    assert row["weekly_hours"] == "Entre 1 y 3 horas a la semana"
    assert row["timezone"] == "Venezuela (GMT-4)"
    assert row["updated_at"] is not None
    assert body["filters"] == [["Búsqueda (correo)", marker]]

    especialistas = await client.get(
        f"{SURVEYS}/especialistas/responses",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    # Mismo código, otra etiqueta: la de la encuesta de especialistas.
    assert especialistas.json()["rows"][0]["roles"].startswith(
        "Atender pacientes directamente en mi especialidad"
    )


async def test_filtro_por_fecha_incluye_el_dia_final(
    client: AsyncClient, anon_client: AsyncClient, super_admin
) -> None:
    marker = _marker()
    await anon_client.post(
        f"{SURVEYS}/psicologos/responses", json=_answers(f"{marker}@example.com")
    )
    hoy = to_local(datetime.now(UTC)).date()

    async def total(**params) -> int:
        resp = await client.get(
            f"{SURVEYS}/psicologos/responses",
            headers=auth_headers(super_admin.id),
            params={"search": marker, **params},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["total"]

    assert await total(answered_from=str(hoy), answered_to=str(hoy)) == 1
    assert await total(answered_from=str(hoy + timedelta(days=1))) == 0
    assert await total(answered_to=str(hoy - timedelta(days=1))) == 0


async def test_empates_de_fecha_se_ordenan_por_id(
    client: AsyncClient, anon_client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    """Dentro de la transacción del test, `now()` es el mismo instante para todas las filas: las
    tres empatan en `updated_at`, que es justo el caso en que el desempate decide. Deben salir en
    el orden de su id, página a página, sin repetirse."""
    marker = _marker()
    emails = [f"{marker}-{i}@example.com" for i in range(3)]
    for email in emails:
        resp = await anon_client.post(f"{SURVEYS}/psicologos/responses", json=_answers(email))
        assert resp.status_code == 201, resp.text

    ids = {}
    for email in emails:
        [fila] = await _stored(db_session, "psicologos", email)
        ids[email] = fila.id
    assert len({(await _stored(db_session, "psicologos", e))[0].updated_at for e in emails}) == 1
    esperado = sorted(emails, key=lambda e: ids[e])

    vistos = []
    for skip in (0, 2):
        resp = await client.get(
            f"{SURVEYS}/psicologos/responses",
            headers=auth_headers(super_admin.id),
            params={"search": marker, "skip": skip, "limit": 2},
        )
        body = resp.json()
        assert body["total"] == 3
        vistos += [r["email"] for r in body["rows"]]
    assert vistos == esperado


# --- Exportación --------------------------------------------------------------


async def test_export_es_un_xlsx_con_las_respuestas_y_queda_en_audit_log(
    client: AsyncClient, anon_client: AsyncClient, super_admin, db_session: AsyncSession
) -> None:
    marker = _marker()
    email = f"{marker}@example.com"
    await anon_client.post(
        f"{SURVEYS}/medicos-generales/responses",
        json=_answers(
            email,
            roles=["rol_activo"],
            role_active_detail="Coordinar",
            timezone=None,
            notes="Guardias rotativas",
        ),
    )

    resp = await client.get(
        f"{SURVEYS}/medicos-generales/responses/export",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == XLSX_MEDIA_TYPE
    hoy = f"{to_local(datetime.now(UTC)):%Y-%m-%d}"
    assert (
        resp.headers["content-disposition"]
        == f'attachment; filename="encuesta-medicos-generales-{hoy}.xlsx"'
    )
    strings = _shared_strings(resp.content)
    assert "Encuesta de médicos generales" in strings
    assert email in strings
    assert "Asumir un rol más activo (coordinar, liderar)" in strings
    assert "Rol más activo: qué tiene en mente" in strings
    # Exactamente la fila del filtro (la 1 es la cabecera).
    assert len(re.findall(r'<row r="(?!1")', _sheet_xml(resp.content))) == 1

    entry = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "report.exported", AuditLog.actor_user_id == super_admin.id)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.resource == "reports"
    assert entry.resource_id == "marketing-medicos-generales"
    assert entry.metadata_["rows"] == 1
    assert marker in str(entry.metadata_["filters"])
    # El filtro queda registrado; las respuestas no.
    assert "Guardias rotativas" not in str(entry.metadata_)


async def test_export_escribe_como_texto_lo_que_parece_una_formula(
    client: AsyncClient, anon_client: AsyncClient, super_admin
) -> None:
    """Cualquiera puede escribir en el formulario público. Si el Excel convirtiera en fórmula un
    texto que empieza por `=`, un `=HYPERLINK(...)` llegaría al super_admin como enlace activo
    dentro de un archivo "de confianza" generado por la plataforma."""
    marker = _marker()
    payload = '=HYPERLINK("https://phishing.example/login","Ver respuesta")'
    await anon_client.post(
        f"{SURVEYS}/psicologos/responses",
        json=_answers(f"{marker}@example.com", notes=payload),
    )

    resp = await client.get(
        f"{SURVEYS}/psicologos/responses/export",
        headers=auth_headers(super_admin.id),
        params={"search": marker},
    )
    assert resp.status_code == 200, resp.text
    assert "<f>" not in _sheet_xml(resp.content), "una celda se escribió como fórmula"
    assert "=HYPERLINK(" in _shared_strings(resp.content)


# --- Totales y gráficos -------------------------------------------------------
# La base local es un restore de producción: estos agregados cuentan TODA la encuesta, no solo lo
# que siembra el test. Por eso se asierta la DIFERENCIA entre antes y después de sembrar, que
# dentro de la transacción del test solo puede venir de lo sembrado.


async def _get_json(client: AsyncClient, url: str, headers: dict, **params) -> dict | list:
    resp = await client.get(url, headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_code(options: list[dict]) -> dict[str, int]:
    return {o["code"]: o["count"] for o in options}


def _delta(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    """Solo lo que cambió, para asertar exactamente qué movió lo sembrado."""
    return {code: n - before.get(code, 0) for code, n in after.items() if n != before.get(code, 0)}


def _matrix_delta(after: dict, before: dict) -> dict[tuple[str, str], int]:
    days = [d["code"] for d in after["days"]]
    moments = [m["code"] for m in after["moments"]]
    return {
        (day, moment): after["availability"][i][j] - before["availability"][i][j]
        for i, day in enumerate(days)
        for j, moment in enumerate(moments)
        if after["availability"][i][j] != before["availability"][i][j]
    }


async def test_totales_y_graficos_solo_para_super_admin(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession, super_admin
) -> None:
    """Son agregados, sin correos, pero salen de las mismas respuestas: mismo permiso que el
    listado."""
    admin = make_profile(role="admin")
    db_session.add(admin)
    await db_session.flush()

    for url in (SURVEYS, f"{SURVEYS}/especialistas/stats"):
        assert (await anon_client.get(url)).status_code == 401
        assert (await client.get(url, headers=auth_headers(admin.id))).status_code == 403
        ok = await client.get(url, headers=auth_headers(super_admin.id))
        assert ok.status_code == 200, f"{url} -> {ok.status_code}: {ok.text}"


async def test_totales_cuentan_cada_encuesta_en_el_orden_de_las_pestanas(
    client: AsyncClient, anon_client: AsyncClient, super_admin
) -> None:
    headers = auth_headers(super_admin.id)
    antes = await _get_json(client, SURVEYS, headers)
    assert [t["survey"] for t in antes] == ["psicologos", "especialistas", "medicos-generales"]

    marker = _marker()
    for i in range(2):
        await anon_client.post(
            f"{SURVEYS}/psicologos/responses", json=_answers(f"{marker}-{i}@example.com")
        )
    await anon_client.post(
        f"{SURVEYS}/medicos-generales/responses", json=_answers(f"{marker}@example.com")
    )
    # Responder otra vez reemplaza: no suma.
    await anon_client.post(
        f"{SURVEYS}/psicologos/responses", json=_answers(f"{marker}-0@example.com")
    )

    despues = await _get_json(client, SURVEYS, headers)
    delta = {d["survey"]: d["total"] - a["total"] for a, d in zip(antes, despues, strict=True)}
    assert delta == {"psicologos": 2, "especialistas": 0, "medicos-generales": 1}


async def test_totales_de_una_encuesta_sin_respuestas_salen_en_cero(db_session: AsyncSession):
    """Sin filas, `GROUP BY` no devuelve la encuesta: el servicio la rellena con 0. Se prueba
    borrando dentro de la transacción del test (se deshace al terminar)."""
    await db_session.execute(
        delete(MarketingSurveyResponse).where(MarketingSurveyResponse.survey == "especialistas")
    )
    totales = await marketing_service.survey_totals(db_session)
    assert [(t.survey, t.total) for t in totales if t.survey == "especialistas"] == [
        ("especialistas", 0)
    ]


async def test_graficos_cuentan_opciones_horas_y_la_cobertura_por_dia_y_momento(
    client: AsyncClient, anon_client: AsyncClient, super_admin
) -> None:
    """Dos psicólogos: la matriz cuenta a cada uno en cada par día×momento que marcó, las horas
    mínimas suman el piso de cada rango y la ubicación se cuenta por opción."""
    headers = auth_headers(super_admin.id)
    url = f"{SURVEYS}/psicologos/stats"
    antes = await _get_json(client, url, headers)

    marker = _marker()
    for email, body in [
        (
            f"{marker}-a@example.com",
            {
                "roles": ["atender_pacientes", "otra"],
                "role_other_detail": "Grupos",
                "days": ["sabado", "lunes"],
                "moments": ["noche", "manana"],
                "weekly_hours": "mas_de_6",
                "timezone": "venezuela",
            },
        ),
        (
            f"{marker}-b@example.com",
            {
                "roles": ["responder_interconsultas"],
                "days": ["sabado"],
                "moments": ["noche"],
                "weekly_hours": "menos_de_1",
                "timezone": "otra",
                "timezone_other": "Japón",
            },
        ),
    ]:
        resp = await anon_client.post(
            f"{SURVEYS}/psicologos/responses", json=_answers(email, **body)
        )
        assert resp.status_code == 201, resp.text

    despues = await _get_json(client, url, headers)
    assert despues["total"] - antes["total"] == 2
    assert _delta(_by_code(despues["roles"]), _by_code(antes["roles"])) == {
        "atender_pacientes": 1,
        "responder_interconsultas": 1,
        "otra": 1,
    }
    assert _delta(_by_code(despues["days"]), _by_code(antes["days"])) == {"lunes": 1, "sabado": 2}
    assert _delta(_by_code(despues["moments"]), _by_code(antes["moments"])) == {
        "manana": 1,
        "noche": 2,
    }
    assert _matrix_delta(despues, antes) == {
        ("lunes", "manana"): 1,
        ("lunes", "noche"): 1,
        ("sabado", "manana"): 1,
        ("sabado", "noche"): 2,
    }
    assert _delta(_by_code(despues["weekly_hours"]), _by_code(antes["weekly_hours"])) == {
        "mas_de_6": 1,
        "menos_de_1": 1,
    }
    assert despues["min_weekly_hours"] - antes["min_weekly_hours"] == 6  # 6 + 0
    assert _delta(_by_code(despues["timezones"]), _by_code(antes["timezones"])) == {
        "venezuela": 1,
        "otra": 1,
    }

    # Todas las opciones, en el orden del formulario y con la etiqueta de ESTA encuesta, aunque
    # tengan 0; y la matriz con una fila por día y una columna por momento.
    survey = marketing_service.SURVEYS["psicologos"]
    assert [(o["code"], o["label"]) for o in despues["roles"]] == list(survey.roles.items())
    assert [o["code"] for o in despues["days"]] == list(marketing_service.DAYS)
    assert [o["code"] for o in despues["weekly_hours"]] == list(marketing_service.WEEKLY_HOURS)
    assert [o["code"] for o in despues["timezones"]] == list(marketing_service.TIMEZONES)
    assert len(despues["availability"]) == len(marketing_service.DAYS)
    assert {len(row) for row in despues["availability"]} == {len(marketing_service.MOMENTS)}
    assert despues["filters"] == []


async def test_graficos_por_forma_de_participar_solo_cuentan_a_quienes_la_marcaron(
    client: AsyncClient, anon_client: AsyncClient, super_admin
) -> None:
    """La cobertura para organizar turnos es la de quienes van a atender, no la de toda la
    encuesta: con `role`, quien solo responde interconsultas no suma."""
    headers = auth_headers(super_admin.id)
    url = f"{SURVEYS}/especialistas/stats"
    params = {"role": "atender_pacientes"}
    antes = await _get_json(client, url, headers, **params)

    marker = _marker()
    await anon_client.post(
        f"{SURVEYS}/especialistas/responses",
        json=_answers(
            f"{marker}-atiende@example.com",
            roles=["atender_pacientes", "coordinar_especialidad"],
            days=["domingo"],
            moments=["tarde"],
            weekly_hours="entre_3_y_6",
        ),
    )
    await anon_client.post(
        f"{SURVEYS}/especialistas/responses",
        json=_answers(
            f"{marker}-responde@example.com",
            roles=["responder_interconsultas"],
            days=["domingo"],
            moments=["tarde"],
            weekly_hours="mas_de_6",
        ),
    )

    despues = await _get_json(client, url, headers, **params)
    assert despues["total"] - antes["total"] == 1
    assert _delta(_by_code(despues["roles"]), _by_code(antes["roles"])) == {
        "atender_pacientes": 1,
        "coordinar_especialidad": 1,
    }
    assert _matrix_delta(despues, antes) == {("domingo", "tarde"): 1}
    assert despues["min_weekly_hours"] - antes["min_weekly_hours"] == 3
    assert despues["filters"] == [
        ["Cómo quieren participar", "Atender pacientes directamente en mi especialidad"]
    ]


@pytest.mark.parametrize(
    ("survey", "role"),
    [
        ("psicologos", "pedir_interconsultas"),  # existe, pero en médicos generales
        ("especialistas", "cualquiera"),
    ],
)
async def test_graficos_con_una_forma_de_participar_ajena_dan_422(
    client: AsyncClient, super_admin, survey: str, role: str
) -> None:
    resp = await client.get(
        f"{SURVEYS}/{survey}/stats", headers=auth_headers(super_admin.id), params={"role": role}
    )
    assert resp.status_code == 422, resp.text
    assert role in resp.json()["detail"]


async def test_graficos_de_medicos_generales_no_traen_ubicacion_y_filtran_por_fecha(
    client: AsyncClient, anon_client: AsyncClient, super_admin
) -> None:
    headers = auth_headers(super_admin.id)
    await anon_client.post(
        f"{SURVEYS}/medicos-generales/responses", json=_answers(f"{_marker()}@example.com")
    )
    hoy = to_local(datetime.now(UTC)).date()

    body = await _get_json(client, f"{SURVEYS}/medicos-generales/stats", headers)
    assert body["timezones"] is None
    assert body["total"] >= 1

    manana = str(hoy + timedelta(days=1))
    futuro = await _get_json(
        client, f"{SURVEYS}/medicos-generales/stats", headers, answered_from=manana
    )
    assert futuro["total"] == 0
    assert futuro["min_weekly_hours"] == 0
    assert all(o["count"] == 0 for o in futuro["roles"])
    assert all(n == 0 for row in futuro["availability"] for n in row)
    assert futuro["filters"] == [["Respondieron desde", manana]]


def test_cada_rango_de_horas_tiene_su_piso() -> None:
    """Las horas mínimas suman `WEEKLY_HOURS_FLOOR.get(código, 0)`: un rango nuevo en el
    formulario sin su piso contaría 0 horas en silencio, y el total bajaría sin que se note."""
    assert set(marketing_service.WEEKLY_HOURS_FLOOR) == set(marketing_service.WEEKLY_HOURS)


def test_opcion_retirada_sigue_contando_al_final_con_su_codigo() -> None:
    """Si mañana se retira una opción, los gráficos no pueden esconder a quienes ya la marcaron."""
    ordered = marketing_service._ordered({"noche": 2, "madrugada": 1}, marketing_service.MOMENTS)
    assert [(o.code, o.label, o.count) for o in ordered] == [
        ("manana", "Mañana", 0),
        ("tarde", "Tarde", 0),
        ("noche", "Noche", 2),
        ("variable", "Es variable", 0),
        ("madrugada", "madrugada", 1),
    ]


# --- Migración del permiso ----------------------------------------------------


async def test_migracion_del_permiso_es_idempotente_y_solo_otorga_a_super_admin(
    db_session: AsyncSession,
) -> None:
    await _apply_permission_migration(db_session)
    await _apply_permission_migration(db_session)  # re-aplicar: no-op

    permisos = (
        (await db_session.execute(select(Permission).where(Permission.code == "marketing.read")))
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
                .where(Permission.code == "marketing.read")
            )
        )
        .scalars()
        .all()
    )
    assert sorted(roles) == ["super_admin"], "marketing.read debe ser exclusivo de super_admin"
