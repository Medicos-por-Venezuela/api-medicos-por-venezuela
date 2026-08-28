"""Preflight CORS: las cabeceras que el frontend manda de verdad deben estar permitidas.

El fallo que motiva esta prueba: `X-Consultation-Token` no estaba en `allow_headers`, así que el
navegador tumbaba el preflight de POST /consultations/{id}/video-room con "Disallowed CORS
headers" y el paciente recién registrado se quedaba sin sala. curl no lo veía (no hace preflight).
"""

import pytest
from httpx import AsyncClient


@pytest.mark.parametrize(
    "header",
    ["authorization", "content-type", "x-correlation-id", "x-consultation-token"],
)
async def test_preflight_permite_las_cabeceras_del_frontend(
    live_client: AsyncClient, header: str
) -> None:
    resp = await live_client.options(
        "/api/v1/consultations/00000000-0000-0000-0000-000000000000/video-room",
        headers={
            "Origin": "https://medicosporvenezuela.org",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": header,
        },
    )
    assert resp.status_code == 200, resp.text
