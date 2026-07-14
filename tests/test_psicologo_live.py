"""Tests de integración reales contra la FPV (requieren internet).

NO se ejecutan en CI. Para correrlos manualmente:
    .venv\\Scripts\\pytest.exe -m live -v
"""

import pytest

from src.services.psicologo import verificar_psicologo


@pytest.mark.live
async def test_psicologo_live_existente():
    """V-21560752 debe estar registrada como psicóloga en la FPV."""
    result = await verificar_psicologo("21560752")

    assert result.encontrado is True, f"Se esperaba encontrado=True, error: {result.error}"
    assert result.nombre, "Debe devolver el nombre"
    assert result.apellido, "Debe devolver el apellido"
    assert result.licencia, "Debe devolver el número de licencia (fpv)"
    assert result.error is None


@pytest.mark.live
async def test_psicologo_live_inexistente():
    """Una cédula inexistente no debe estar registrada en la FPV."""
    result = await verificar_psicologo("99999999")

    assert result.encontrado is False
    assert result.nombre is None
    assert result.apellido is None
    assert result.licencia is None
    assert result.error is not None
