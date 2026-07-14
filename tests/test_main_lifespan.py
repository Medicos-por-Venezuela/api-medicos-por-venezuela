"""Prueba del guard de arranque que falla rápido en producción con secretos por
defecto. Cubre el nuevo check de `SUPABASE_SERVICE_ROLE_KEY` (mismo patrón que el
de `SUPABASE_JWT_SECRET`, ya existente)."""

import pytest

import src.main as main_module


async def test_lifespan_falla_en_prod_si_service_role_key_default(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    monkeypatch.setattr(
        main_module.settings,
        "SUPABASE_SERVICE_ROLE_KEY",
        main_module._INSECURE_SERVICE_ROLE_DEFAULT,
    )

    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_ROLE_KEY"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_ok_en_prod_con_service_role_key_configurado(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    monkeypatch.setattr(
        main_module.settings, "SUPABASE_SERVICE_ROLE_KEY", "un-service-role-key-real-de-produccion"
    )

    async with main_module.lifespan(main_module.app):
        pass
