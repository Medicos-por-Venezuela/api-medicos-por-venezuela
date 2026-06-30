"""Tests del servicio SACS y su endpoint.

La llamada HTTP al SACS se mockea con unittest.mock para evitar dependencias
de red en CI y no consumir el servicio externo en cada ejecución.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.services.sacs import verificar_sacs

PREFIX = "/api/v1/verificacion-sacs"

# --- Respuestas XML de muestra del SACS ---

_XML_MEDICO = (
    """xajax_userTable('{"nombre1":"JUAN","apellido1":"PEREZ"}')"""
    """xajax_tableProfesion('[{"profesion":"M&Eacute;DICO","licencia":"MP-12345"}]')"""
)
_XML_NO_MEDICO = (
    """xajax_userTable('{"nombre1":"ANA","apellido1":"GOMEZ"}')"""
    """xajax_tableProfesion('[{"profesion":"ENFERMERA","licencia":"EN-99999"}]')"""
)
_XML_SIN_PROFESIONES = (
    """xajax_userTable('{"nombre1":"PEDRO","apellido1":"LOPEZ"}')"""
    """xajax_tableProfesion('[]')"""
)
_XML_NO_EXISTE = (
    """xajax_userTable('""')"""
    """xajax_tableProfesion('[]')"""
)


def _mock_httpx(xml_text: str):
    """Parchea httpx.AsyncClient para devolver `xml_text` como respuesta del SACS."""
    mock_resp = MagicMock()
    mock_resp.text = xml_text
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    return patch("src.services.sacs.httpx.AsyncClient", return_value=ctx)


# --- Tests del servicio (sin red, sin BD) ---


async def test_sacs_medico_encontrado():
    with _mock_httpx(_XML_MEDICO):
        result = await verificar_sacs("V-21369660")

    assert result.encontrado is True
    assert result.es_medico is True
    assert result.nombre == "JUAN"
    assert result.apellido == "PEREZ"
    assert result.profesion == "MÉDICO"
    assert result.licencia == "MP-12345"
    assert result.error is None


async def test_sacs_no_es_medico():
    with _mock_httpx(_XML_NO_MEDICO):
        result = await verificar_sacs("V-11111111")

    assert result.encontrado is True
    assert result.es_medico is False
    assert result.profesion == "ENFERMERA"


async def test_sacs_cedula_no_existe():
    with _mock_httpx(_XML_NO_EXISTE):
        result = await verificar_sacs("V-99999999")

    assert result.encontrado is False
    assert "no está registrada" in (result.error or "")


async def test_sacs_sin_profesiones():
    # profRaw == '[]' entra en el check de vacío → mismo comportamiento que el JS original.
    with _mock_httpx(_XML_SIN_PROFESIONES):
        result = await verificar_sacs("V-12345678")

    assert result.encontrado is False
    assert "no está registrada" in (result.error or "")


async def test_sacs_formato_invalido_sin_prefijo():
    result = await verificar_sacs("21369660")
    assert result.encontrado is False
    assert "Formato inválido" in (result.error or "")


async def test_sacs_formato_invalido_prefijo_incorrecto():
    result = await verificar_sacs("P-21369660")
    assert result.encontrado is False
    assert "Formato inválido" in (result.error or "")


async def test_sacs_normaliza_mayusculas_y_espacios():
    with _mock_httpx(_XML_MEDICO):
        result = await verificar_sacs("  v-21369660  ")
    assert result.encontrado is True


async def test_sacs_respuesta_inesperada():
    with _mock_httpx("<html>Error del servidor</html>"):
        result = await verificar_sacs("V-12345678")

    assert result.encontrado is False
    assert "inesperada" in (result.error or "")


async def test_sacs_error_http():
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    http_err = httpx.HTTPStatusError("503", request=MagicMock(), response=mock_resp)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=http_err)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("src.services.sacs.httpx.AsyncClient", return_value=ctx):
        result = await verificar_sacs("V-12345678")

    assert result.encontrado is False
    assert "503" in (result.error or "")


async def test_sacs_error_conexion():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("timeout"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("src.services.sacs.httpx.AsyncClient", return_value=ctx):
        result = await verificar_sacs("V-12345678")

    assert result.encontrado is False
    assert "conexión" in (result.error or "")


# --- Tests del endpoint HTTP ---


async def test_sacs_endpoint_publico_sin_token(live_client):
    """Endpoint público: sin token debe responder 200, no 401."""
    with _mock_httpx(_XML_MEDICO):
        resp = await live_client.get(f"{PREFIX}/V-21369660")
    assert resp.status_code == 200
    assert resp.json()["encontrado"] is True


async def test_sacs_endpoint_con_token(client):
    """Con token de admin también funciona."""
    with _mock_httpx(_XML_MEDICO):
        resp = await client.get(f"{PREFIX}/V-21369660")
    assert resp.status_code == 200
    assert resp.json()["es_medico"] is True


async def test_sacs_endpoint_formato_invalido_devuelve_200(live_client):
    """Formato inválido → 200 con encontrado=false (el error es de negocio, no HTTP)."""
    resp = await live_client.get(f"{PREFIX}/INVALIDO")
    assert resp.status_code == 200
    assert resp.json()["encontrado"] is False
