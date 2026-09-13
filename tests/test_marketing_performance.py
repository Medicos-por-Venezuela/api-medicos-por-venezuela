"""Pruebas del rendimiento de la campaña (`GET /marketing/performance`): Kit + respuestas.

Kit nunca se llama de verdad: el `.env` local puede tener una clave real, así que un fixture
automático la vacía y cambia el transporte por uno que revienta. Las pruebas que necesitan a Kit
usan `fake_kit`, un `httpx.MockTransport` con envíos inventados.

Tres cosas que se defienden:

1. Cada envío cae en SU encuesta (por el enlace o, sin clics, por la plantilla), y lo que no es de
   las encuestas —otro boletín, un borrador— no suma.
2. La tasa de respuesta cuenta solo lo llegado después del primer envío.
3. Sin clave o con Kit caído el panel responde igual, y la clave no aparece en los logs.

La base local es un restore de producción: los conteos de respuestas se asertan como diferencia
entre antes y después de sembrar.
"""

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.tz import VET
from src.models.marketing_survey_response import MarketingSurveyResponse
from src.services import kit
from src.services.marketing import SURVEYS
from tests._helpers import auth_headers, make_profile
from tests.test_marketing import _apply_permission_migration

URL = "/api/v1/marketing/performance"
KEY = "kit_test_clave_que_no_debe_aparecer_en_ningun_log"


@pytest.fixture(autouse=True)
def _sin_kit_real(monkeypatch):
    def prohibido(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Un test intentó llamar a Kit: {request.url.path}")

    monkeypatch.setattr(settings, "KIT_API_KEY", "")
    monkeypatch.setattr(kit, "_transport", httpx.MockTransport(prohibido))
    kit.clear_cache()
    yield
    kit.clear_cache()


@pytest.fixture
async def super_admin(db_session: AsyncSession):
    await _apply_permission_migration(db_session)
    profile = make_profile(role="super_admin")
    db_session.add(profile)
    await db_session.flush()
    return profile


class FakeKit:
    """Kit de mentira: sirve los envíos, sus clics y su plantilla, y anota cada petición."""

    def __init__(self) -> None:
        self.broadcasts: list[dict] = []
        self.clicks: dict[int, list[str]] = {}
        self.templates: dict[int, str] = {}
        self.requests: list[httpx.Request] = []
        self.fail: httpx.Response | Exception | None = None

    def add(
        self,
        broadcast_id: int,
        *,
        hours_ago: float | None,
        recipients: int = 100,
        opened: int = 40,
        click_rate: float = 5.0,
        unsubscribes: int = 0,
        status: str = "completed",
        links: list[str] | None = None,
        template: str = "",
    ) -> datetime | None:
        sent_at = datetime.now(UTC) - timedelta(hours=hours_ago) if hours_ago is not None else None
        self.broadcasts.append(
            {
                "id": broadcast_id,
                "subject": f"Envío {broadcast_id}",
                "send_at": sent_at.isoformat().replace("+00:00", "Z") if sent_at else None,
                "stats": {
                    "recipients": recipients,
                    "open_rate": round(opened / recipients * 100, 2),
                    "emails_opened": opened,
                    "click_rate": click_rate,
                    "unsubscribe_rate": 0.0,
                    "unsubscribes": unsubscribes,
                    "total_clicks": 0,
                    "show_total_clicks": True,
                    "status": status,
                    "progress": 100.0,
                    "open_tracking_disabled": False,
                    "click_tracking_disabled": False,
                },
            }
        )
        self.clicks[broadcast_id] = links or []
        self.templates[broadcast_id] = template
        return sent_at

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail is not None:
            return self.fail
        path = request.url.path
        if path.endswith("/broadcasts/stats"):
            return httpx.Response(200, json={"broadcasts": self.broadcasts, "pagination": {}})
        if match := re.search(r"/broadcasts/(\d+)/clicks$", path):
            links = self.clicks[int(match.group(1))]
            rows = [
                {"id": i, "url": url, "unique_clicks": 1, "click_to_delivery_rate": 0.01}
                for i, url in enumerate(links)
            ]
            return httpx.Response(200, json={"broadcast": {"clicks": rows}, "pagination": {}})
        if match := re.search(r"/broadcasts/(\d+)$", path):
            name = self.templates[int(match.group(1))]
            return httpx.Response(
                200, json={"broadcast": {"email_template": {"id": 1, "name": name}}}
            )
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def fake_kit(monkeypatch) -> FakeKit:
    fake = FakeKit()
    monkeypatch.setattr(settings, "KIT_API_KEY", KEY)
    monkeypatch.setattr(kit, "_transport", httpx.MockTransport(fake.handler))
    return fake


async def _performance(client: AsyncClient, user, **params) -> dict:
    resp = await client.get(URL, headers=auth_headers(user.id), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _survey(body: dict, slug: str) -> dict:
    return next(s for s in body["surveys"] if s["survey"] == slug)


def _response(survey: str, created_at: datetime) -> MarketingSurveyResponse:
    return MarketingSurveyResponse(
        survey=survey,
        email=f"kit{uuid.uuid4().hex[:10]}@example.com",
        roles=["atender_pacientes"],
        moments=["noche"],
        days=["lunes"],
        weekly_hours="entre_1_y_3",
        timezone="venezuela" if survey != "medicos-generales" else None,
        created_at=created_at,
        updated_at=created_at,
    )


def _survey_link(slug: str) -> str:
    # Así llega la URL de Kit: con el correo de un destinatario en la query.
    return f"https://medicosporvenezuela.org/encuesta/{slug}?email=ana.perez@example.com"


# --- Embudo -------------------------------------------------------------------


async def test_cada_envio_cae_en_su_encuesta_y_lo_ajeno_no_suma(
    client: AsyncClient, super_admin, fake_kit: FakeKit
) -> None:
    fake_kit.add(
        101,
        hours_ago=6,
        recipients=1000,
        opened=400,
        click_rate=7.5,
        unsubscribes=2,
        links=[_survey_link("especialistas")],
    )
    # Sin clics todavía: se asocia por la plantilla, con tilde y mayúsculas.
    fake_kit.add(
        102,
        hours_ago=5,
        recipients=200,
        opened=50,
        click_rate=3.5,
        template="Agradecimientos Psicólogos",
    )
    fake_kit.add(
        103,
        hours_ago=2,
        recipients=100,
        opened=30,
        click_rate=5.0,
        links=[_survey_link("psicologos")],
    )
    fake_kit.add(
        104, hours_ago=4, links=["https://medicosporvenezuela.org/blog/uno"], template="Boletín"
    )
    fake_kit.add(105, hours_ago=None, status="draft", template="Especialistas")

    body = await _performance(client, super_admin)
    assert body["kit_status"] == "ok"
    assert body["kit_fetched_at"] is not None
    assert [s["survey"] for s in body["surveys"]] == list(SURVEYS)

    especialistas = _survey(body, "especialistas")
    assert [c["id"] for c in especialistas["campaigns"]] == [101]
    assert (
        especialistas["recipients"],
        especialistas["opened"],
        especialistas["clicked"],
        especialistas["unsubscribed"],
    ) == (1000, 400, 75, 2)

    # Dos envíos de la misma encuesta se suman, del más antiguo al más reciente.
    psicologos = _survey(body, "psicologos")
    assert [c["id"] for c in psicologos["campaigns"]] == [102, 103]
    assert (psicologos["recipients"], psicologos["opened"], psicologos["clicked"]) == (300, 80, 12)

    medicos = _survey(body, "medicos-generales")
    assert medicos["campaigns"] == []
    assert medicos["recipients"] is None  # sin envíos: no se sabe, no es 0
    assert medicos["responses_after_send"] is None
    assert medicos["first_sent_at"] is None

    # El borrador no se consulta, y la clave viaja en la cabecera de todas las peticiones.
    assert not [p for p in fake_kit.paths() if "/105" in p]
    assert {r.headers["X-Kit-Api-Key"] for r in fake_kit.requests} == {KEY}


async def test_la_respuesta_no_filtra_correos_de_las_urls_de_kit(
    client: AsyncClient, super_admin, fake_kit: FakeKit
) -> None:
    fake_kit.add(101, hours_ago=3, links=[_survey_link("especialistas")])
    resp = await client.get(URL, headers=auth_headers(super_admin.id))
    assert resp.status_code == 200, resp.text
    assert "ana.perez@example.com" not in resp.text
    assert "email=" not in resp.text


async def test_la_tasa_de_respuesta_solo_cuenta_lo_llegado_despues_del_primer_envio(
    client: AsyncClient, super_admin, fake_kit: FakeKit, db_session: AsyncSession
) -> None:
    fake_kit.add(101, hours_ago=3, links=[_survey_link("especialistas")])
    antes = _survey(await _performance(client, super_admin), "especialistas")

    now = datetime.now(UTC)
    db_session.add_all(
        [
            _response("especialistas", now - timedelta(hours=5)),  # prueba previa al envío
            _response("especialistas", now - timedelta(hours=1)),
        ]
    )
    await db_session.flush()

    despues = _survey(await _performance(client, super_admin), "especialistas")
    assert despues["responses"] - antes["responses"] == 2
    assert despues["responses_after_send"] - antes["responses_after_send"] == 1


# --- Sin Kit ------------------------------------------------------------------


async def test_sin_clave_el_panel_sigue_con_lo_que_sabe_la_plataforma(
    client: AsyncClient, super_admin
) -> None:
    body = await _performance(client, super_admin)
    assert body["kit_status"] == "not_configured"
    assert body["kit_fetched_at"] is None
    for survey in body["surveys"]:
        assert survey["campaigns"] == []
        assert survey["recipients"] is None
        assert isinstance(survey["responses"], int)
    timeline = body["timeline"]
    assert set(timeline["responses"]) == set(SURVEYS)
    assert all(len(serie) == len(timeline["buckets"]) for serie in timeline["responses"].values())


@pytest.mark.parametrize(
    "fallo",
    [
        httpx.Response(401, json={"errors": ["The access token is invalid"]}),
        httpx.Response(500),
        httpx.Response(200, json={"inesperado": True}),
        httpx.ConnectTimeout("Kit no contesta"),
    ],
    ids=["clave-rechazada", "error-500", "json-inesperado", "timeout"],
)
async def test_kit_caido_no_tumba_el_panel_ni_filtra_la_clave(
    client: AsyncClient, super_admin, fake_kit: FakeKit, caplog, fallo
) -> None:
    fake_kit.fail = fallo
    with caplog.at_level(logging.DEBUG):
        body = await _performance(client, super_admin)
    assert body["kit_status"] == "unavailable"
    assert all(s["recipients"] is None for s in body["surveys"])
    assert KEY not in caplog.text


async def test_las_metricas_se_reutilizan_y_actualizar_tiene_un_minimo(
    client: AsyncClient, super_admin, fake_kit: FakeKit, monkeypatch
) -> None:
    """Cada carga del panel serían varias peticiones a Kit: se reutilizan unos minutos, y ni el
    botón de actualizar puede pedirlas más de una vez cada 30 segundos."""
    reloj = [1000.0]
    monkeypatch.setattr(kit.time, "monotonic", lambda: reloj[0])
    fake_kit.add(101, hours_ago=3, links=[_survey_link("especialistas")])

    def listados() -> int:
        return sum(p.endswith("/broadcasts/stats") for p in fake_kit.paths())

    await _performance(client, super_admin)
    assert listados() == 1
    reloj[0] += 60
    await _performance(client, super_admin)
    assert listados() == 1  # caché
    reloj[0] += 10
    await _performance(client, super_admin, refresh="true")
    assert listados() == 2  # 70 s desde la última: actualizar sí va a Kit
    reloj[0] += 10
    await _performance(client, super_admin, refresh="true")
    assert listados() == 2  # 10 s: todavía no
    reloj[0] += settings.KIT_STATS_CACHE_SECONDS + 1
    await _performance(client, super_admin)
    assert listados() == 3  # caducó


# --- Línea de tiempo ----------------------------------------------------------


def _conteos(body: dict, slug: str) -> dict[str, int]:
    timeline = body["timeline"]
    return dict(zip(timeline["buckets"], timeline["responses"][slug], strict=True))


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def test_linea_de_tiempo_por_horas_desde_el_primer_envio(
    client: AsyncClient, super_admin, fake_kit: FakeKit, db_session: AsyncSession
) -> None:
    sent_at = fake_kit.add(101, hours_ago=5.5, links=[_survey_link("psicologos")])
    antes = await _performance(client, super_admin)

    llegada = datetime.now(UTC) - timedelta(minutes=90)
    db_session.add(_response("psicologos", llegada))
    await db_session.flush()
    despues = await _performance(client, super_admin)

    timeline = despues["timeline"]
    assert timeline["granularity"] == "hour"
    buckets = [datetime.fromisoformat(b) for b in timeline["buckets"]]
    assert buckets[0] == sent_at.replace(minute=0, second=0, microsecond=0)
    assert all(b - a == timedelta(hours=1) for a, b in zip(buckets, buckets[1:], strict=False))

    tramo = _iso(llegada.replace(minute=0, second=0, microsecond=0))
    cambios = {
        b: n - _conteos(antes, "psicologos").get(b, 0)
        for b, n in _conteos(despues, "psicologos").items()
        if n != _conteos(antes, "psicologos").get(b, 0)
    }
    assert cambios == {tramo: 1}


async def test_linea_de_tiempo_por_dias_con_dias_de_caracas(
    client: AsyncClient, super_admin, fake_kit: FakeKit, db_session: AsyncSession
) -> None:
    """Pasadas 72 horas va por días, y el día es el de Caracas: empieza a las 04:00 UTC."""
    fake_kit.add(101, hours_ago=24 * 10, links=[_survey_link("medicos-generales")])
    antes = await _performance(client, super_admin)

    llegada = datetime.now(UTC) - timedelta(days=3)
    db_session.add(_response("medicos-generales", llegada))
    await db_session.flush()
    despues = await _performance(client, super_admin)

    timeline = despues["timeline"]
    assert timeline["granularity"] == "day"
    buckets = [datetime.fromisoformat(b) for b in timeline["buckets"]]
    assert all(b.astimezone(VET).hour == 0 for b in buckets)
    assert all(b - a == timedelta(days=1) for a, b in zip(buckets, buckets[1:], strict=False))

    dia = llegada.astimezone(VET).replace(hour=0, minute=0, second=0, microsecond=0)
    tramo = _iso(dia.astimezone(UTC))
    base = _conteos(antes, "medicos-generales")
    cambios = {
        b: n - base.get(b, 0)
        for b, n in _conteos(despues, "medicos-generales").items()
        if n != base.get(b, 0)
    }
    assert cambios == {tramo: 1}


# --- Autorización y asociación ------------------------------------------------


async def test_rendimiento_solo_para_super_admin(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession, super_admin
) -> None:
    admin = make_profile(role="admin")
    db_session.add(admin)
    await db_session.flush()
    assert (await anon_client.get(URL)).status_code == 401
    assert (await client.get(URL, headers=auth_headers(admin.id))).status_code == 403
    assert (await client.get(URL, headers=auth_headers(super_admin.id))).status_code == 200


@pytest.mark.parametrize(
    ("nombre", "encuesta"),
    [
        ("Agradecimientos Psicologos", "psicologos"),
        ("PSICÓLOGOS – recordatorio", "psicologos"),
        ("Especialistas", "especialistas"),
        ("Medicos Generales", "medicos-generales"),
        ("Boletín de septiembre", None),
        ("", None),
    ],
)
def test_la_plantilla_se_asocia_sin_importar_tildes_ni_mayusculas(
    nombre: str, encuesta: str | None
) -> None:
    assert kit.survey_from_template(nombre) == encuesta
