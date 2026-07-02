"""Tests de integración reales contra el SACS (requieren internet).

NO se ejecutan en CI. Para correrlos manualmente:
    .venv\\Scripts\\pytest.exe -m live -v
"""

import pytest

from src.services.sacs import verificar_sacs


@pytest.mark.live
async def test_sacs_live_medico_existente():
    """V-21369660 debe estar registrada y ser médico."""
    result = await verificar_sacs("V-21369660")

    assert result.encontrado is True, f"Se esperaba encontrado=True, error: {result.error}"
    assert result.es_medico is True, f"Se esperaba es_medico=True, profesion: {result.profesion}"
    assert result.nombre, "Debe devolver el nombre"
    assert result.apellido, "Debe devolver el apellido"
    assert result.licencia, "Debe devolver el número de licencia"
    assert result.error is None


@pytest.mark.live
async def test_sacs_live_cedula_inexistente():
    """V-99999999 no debe estar registrada en el SACS."""
    result = await verificar_sacs("V-99999999")

    assert result.encontrado is False
    assert result.es_medico is None
    assert result.nombre is None
    assert result.apellido is None
    assert result.licencia is None
    assert result.error is not None
