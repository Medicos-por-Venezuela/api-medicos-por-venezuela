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


async def test_content_disposition_queda_expuesto_al_navegador(live_client: AsyncClient) -> None:
    """El nombre del archivo de los reportes viaja en `Content-Disposition`, y una respuesta
    cross-origin solo deja leer esa cabecera si está en `Access-Control-Expose-Headers`. Sin
    esto la descarga funciona pero el archivo llega con un nombre inventado por el cliente."""
    resp = await live_client.get(
        "/api/v1/stats/public", headers={"Origin": "https://medicosporvenezuela.org"}
    )
    expuestas = resp.headers.get("access-control-expose-headers", "").lower()
    assert "content-disposition" in expuestas
