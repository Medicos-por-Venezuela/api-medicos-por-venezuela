"""IP real del cliente detrás de Caddy (docs/proxy-e-ip-real.md).

El audit_log (`client_ip`) y el rate limit (`get_remote_address`) leen `request.client.host`.
Detrás de Caddy eso es la IP del proxy salvo que uvicorn confíe en su X-Forwarded-For, y solo
debe confiar en Caddy: si no, cualquiera que llegue al puerto directo elige su IP.

Los valores salen de docker-compose.prod.yml y pasan por la misma ruta que en prod: uvicorn lee
FORWARDED_ALLOW_IPS del entorno y envuelve la app en su ProxyHeadersMiddleware.
"""

import ipaddress
from pathlib import Path

import pytest
import uvicorn
import yaml
from httpx import ASGITransport, AsyncClient
from slowapi.util import get_remote_address
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.core.observability import client_ip

COMPOSE_PROD = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "docker-compose.prod.yml").read_text(encoding="utf-8")
)
API = COMPOSE_PROD["services"]["api"]
FORWARDED_ALLOW_IPS = API["environment"]["FORWARDED_ALLOW_IPS"]

REAL_CLIENT = "203.0.113.7"  # IP pública del paciente/médico (TEST-NET-3)
OTHER_ORIGIN = "198.51.100.9"  # alguien que llega al puerto sin pasar por Caddy (TEST-NET-2)


def _gateway_of_api_network() -> str:
    network = COMPOSE_PROD["networks"][API["networks"][0]]
    return network["ipam"]["config"][0]["gateway"]


async def _whoami(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "client_ip": client_ip(request),
            "rate_limit_key": get_remote_address(request),
            "base_url": str(request.base_url),
        }
    )


def _app_como_en_prod(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", FORWARDED_ALLOW_IPS)
    config = uvicorn.Config(Starlette(routes=[Route("/whoami", _whoami)]), log_config=None)
    config.load()
    return config.loaded_app


async def _whoami_desde(app: object, peer: str, xff: str | None) -> dict[str, str]:
    # Caddy manda las dos cabeceras; el tramo Caddy -> uvicorn es http plano.
    headers = {"X-Forwarded-For": xff, "X-Forwarded-Proto": "https"} if xff is not None else {}
    transport = ASGITransport(app=app, client=(peer, 50000))
    async with AsyncClient(transport=transport, base_url="http://api") as client:
        resp = await client.get("/whoami", headers=headers)
    assert resp.status_code == 200
    return resp.json()


def test_solo_se_confia_en_el_gateway_de_la_red_de_la_api() -> None:
    # Caddy (en el host) llega al contenedor desde el gateway de la red `api`.
    assert FORWARDED_ALLOW_IPS.strip() != "*"
    trusted = [ipaddress.ip_address(h.strip()) for h in FORWARDED_ALLOW_IPS.split(",")]
    assert trusted == [ipaddress.ip_address(_gateway_of_api_network())]


def test_el_puerto_de_la_api_solo_se_publica_en_loopback() -> None:
    assert API["ports"], "sin puerto publicado Caddy no llega a la API"
    for mapping in API["ports"]:
        assert str(mapping).startswith("127.0.0.1:"), mapping


async def test_desde_caddy_se_registra_la_ip_real(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app_como_en_prod(monkeypatch)
    body = await _whoami_desde(app, _gateway_of_api_network(), REAL_CLIENT)
    assert body["client_ip"] == REAL_CLIENT
    assert body["rate_limit_key"] == REAL_CLIENT
    # La URL del feed de la agenda (routers/agenda.py) se arma con base_url: debe salir https.
    assert body["base_url"] == "https://api/"


async def test_una_ip_falsa_puesta_por_el_cliente_no_gana(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El cliente manda su propio X-Forwarded-For y el proxy añade la IP real al final: vale la
    # última que no es de confianza, no la que eligió el cliente.
    app = _app_como_en_prod(monkeypatch)
    body = await _whoami_desde(app, _gateway_of_api_network(), f"10.9.9.9, {REAL_CLIENT}")
    assert body["client_ip"] == REAL_CLIENT


async def test_desde_otro_origen_se_ignora_la_cabecera(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app_como_en_prod(monkeypatch)
    body = await _whoami_desde(app, OTHER_ORIGIN, REAL_CLIENT)
    assert body["client_ip"] == OTHER_ORIGIN
    assert body["rate_limit_key"] == OTHER_ORIGIN
    assert body["base_url"] == "http://api/"


async def test_sin_cabecera_queda_la_ip_del_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    # El health check de deploy.sh (curl a localhost:8000) no pasa por Caddy.
    app = _app_como_en_prod(monkeypatch)
    gateway = _gateway_of_api_network()
    assert (await _whoami_desde(app, gateway, None))["client_ip"] == gateway
