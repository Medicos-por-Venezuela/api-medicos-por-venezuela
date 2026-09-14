"""Tests del servicio SACS y su endpoint.

La llamada HTTP al SACS se mockea con unittest.mock para evitar dependencias
de red en CI y no consumir el servicio externo en cada ejecución.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.schemas.sacs import NO_ENCONTRADO, SERVICIO_NO_DISPONIBLE
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
# Cuerpo real (HTTP 200) que devolvió el SACS el 2026-09-14 para una cédula que no está: oculta
# las dos tablas y avisa, sin llamar a xajax_userTable. No repite la cédula consultada.
_XML_NO_REGISTRADA = (
    '<?xml version="1.0" encoding="UTF-8" ?><xjx>'
    """<cmd n="js"><![CDATA[$('#divTablaProfesiones').hide();]]></cmd>"""
    """<cmd n="js"><![CDATA[$('#divTabla').hide();]]></cmd>"""
    """<cmd n="js"><![CDATA[Swal.fire({title: 'Información', text: 'LA CÉDULA O MATRÍCULA NO """
    """CORRESPONDE CON EL TIPO DE BÚSQUEDA', icon: 'info'});]]></cmd></xjx>"""
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


async def test_sacs_no_registrada_respuesta_real():
    """El SACS no dice "no existe" con datos vacíos: oculta las tablas. Antes esto caía en
    "Respuesta inesperada" y al médico le llegaba el correo de "servicio no disponible"."""
    with _mock_httpx(_XML_NO_REGISTRADA):
        result = await verificar_sacs("V-99999999")

    assert result.encontrado is False
    assert result.error_kind == NO_ENCONTRADO
    assert "no está registrada" in (result.error or "")


@pytest.mark.parametrize(
    "xml_text",
    [
        "<html>Error del servidor</html>",
        '<?xml version="1.0" encoding="UTF-8" ?><xjx></xjx>',
        # Solo una de las dos tablas oculta.
        """<xjx><cmd n="js"><![CDATA[$('#divTabla').hide();]]></cmd></xjx>""",
        # Los mismos scripts fuera de una respuesta xajax (p.ej. la página HTML entera).
        "<html><script>$('#divTablaProfesiones').hide();$('#divTabla').hide();</script></html>",
        # Tablas ocultas pero con datos a medias: no es un "no está".
        """<xjx><cmd n="js"><![CDATA[$('#divTablaProfesiones').hide();$('#divTabla').hide();"""
        """xajax_userTable('{"nombre1":"JUAN"}')]]></cmd></xjx>""",
    ],
    ids=["html", "xjx_vacio", "una_tabla", "sin_sobre_xjx", "user_sin_profesion"],
)
async def test_sacs_respuesta_inesperada(xml_text):
    with _mock_httpx(xml_text):
        result = await verificar_sacs("V-12345678")

    assert result.encontrado is False
    assert result.error_kind == SERVICIO_NO_DISPONIBLE
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


async def test_sacs_endpoint_formato_invalido_devuelve_422(live_client):
    """Formato inválido → 422 rechazado por la validación del path antes del servicio."""
    for cedula_mala in ["INVALIDO", "21369660", "P-21369660", "V21369660"]:
        resp = await live_client.get(f"{PREFIX}/{cedula_mala}")
        assert resp.status_code == 422, f"Se esperaba 422 para '{cedula_mala}'"
