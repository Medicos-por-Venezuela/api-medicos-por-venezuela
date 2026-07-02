"""Tests del servicio de verificación de psicólogos (FPV) y su endpoint.

La llamada HTTP a la FPV se mockea para no depender de la red en CI.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.services.psicologo import verificar_psicologo

PREFIX = "/api/v1/verificacion-psicologo"

# --- Respuestas JSON de muestra de la FPV ---

_JSON_ENCONTRADO = {
    "status": 200,
    "data": {
        "items": [
            {
                "id": 13541,
                "fpv": "13579",
                "cedula": "21560752",
                "primerNombre": "Yolfrancis",
                "primerApellido": "Escalona",
                "denominacionTitulo": "Licenciado(a) en Psicología",
            }
        ],
        "count": 1,
    },
    "errors": [],
}

_JSON_VACIO = {
    "status": 200,
    "data": {"items": [], "count": 0},
    "errors": [],
}


def _mock_httpx(json_data: dict):
    """Parchea httpx.AsyncClient para devolver `json_data` como respuesta de la FPV."""
    mock_resp = MagicMock()
    mock_resp.json = MagicMock(return_value=json_data)
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    return patch("src.services.psicologo.httpx.AsyncClient", return_value=ctx)


# --- Tests del servicio ---


async def test_psicologo_encontrado():
    with _mock_httpx(_JSON_ENCONTRADO):
        result = await verificar_psicologo("21560752")

    assert result.encontrado is True
    assert result.nombre == "YOLFRANCIS"
    assert result.apellido == "ESCALONA"
    assert result.licencia == "13579"
    assert result.error is None


async def test_psicologo_no_encontrado():
    with _mock_httpx(_JSON_VACIO):
        result = await verificar_psicologo("99999999")

    assert result.encontrado is False
    assert result.nombre is None
    assert result.apellido is None
    assert result.licencia is None
    assert "no está registrada" in (result.error or "")


async def test_psicologo_normaliza_prefijo_cedula():
    captured = {}

    mock_resp = MagicMock()
    mock_resp.json = MagicMock(return_value=_JSON_ENCONTRADO)
    mock_resp.raise_for_status = MagicMock()

    async def _capture_get(url, params=None):
        captured["params"] = params
        return mock_resp

    mock_client = AsyncMock()
    mock_client.get = _capture_get
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("src.services.psicologo.httpx.AsyncClient", return_value=ctx):
        await verificar_psicologo("V-21560752")

    # El prefijo V- se elimina antes de llamar a la FPV.
    assert captured["params"]["cedula"] == "21560752"


async def test_psicologo_respuesta_no_json():
    mock_resp = MagicMock()
    mock_resp.json = MagicMock(side_effect=ValueError("no json"))
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("src.services.psicologo.httpx.AsyncClient", return_value=ctx):
        result = await verificar_psicologo("21560752")

    assert result.encontrado is False
    assert "inesperada" in (result.error or "")


async def test_psicologo_error_http():
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    http_err = httpx.HTTPStatusError("503", request=MagicMock(), response=mock_resp)
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=http_err)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("src.services.psicologo.httpx.AsyncClient", return_value=ctx):
        result = await verificar_psicologo("21560752")

    assert result.encontrado is False
    assert "503" in (result.error or "")


async def test_psicologo_error_conexion():
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("src.services.psicologo.httpx.AsyncClient", return_value=ctx):
        result = await verificar_psicologo("21560752")

    assert result.encontrado is False
    assert "conexión" in (result.error or "")


# --- Tests del endpoint HTTP ---


async def test_psicologo_endpoint_publico_sin_token(live_client):
    """Endpoint público: sin token debe responder 200."""
    with _mock_httpx(_JSON_ENCONTRADO):
        resp = await live_client.get(f"{PREFIX}/21560752")
    assert resp.status_code == 200
    data = resp.json()
    assert data["encontrado"] is True
    assert data["licencia"] == "13579"


async def test_psicologo_endpoint_formato_invalido_devuelve_422(live_client):
    """Cédula con letras o demasiado corta → 422 antes del servicio."""
    for cedula_mala in ["ABC", "123", "21560752X"]:
        resp = await live_client.get(f"{PREFIX}/{cedula_mala}")
        assert resp.status_code == 422, f"Se esperaba 422 para '{cedula_mala}'"
