"""Pruebas de los guards de arranque que fallan rápido en producción con configuración
insegura: `SUPABASE_SERVICE_ROLE_KEY` por defecto (mismo patrón que el de
`SUPABASE_JWT_SECRET`, ya existente) y `BACKEND_CORS_ORIGINS` en '*', que con
allow_credentials=True aceptaría credenciales desde cualquier origen."""

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
    # En prod tambien hay que dar origenes CORS explicitos, o salta el guard de abajo.
    monkeypatch.setattr(
        main_module.settings, "BACKEND_CORS_ORIGINS", "https://medicosporvenezuela.org"
    )

    async with main_module.lifespan(main_module.app):
        pass


async def test_lifespan_falla_en_prod_si_cors_es_wildcard(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    monkeypatch.setattr(
        main_module.settings, "SUPABASE_SERVICE_ROLE_KEY", "un-service-role-key-real-de-produccion"
    )
    monkeypatch.setattr(main_module.settings, "BACKEND_CORS_ORIGINS", "*")

    with pytest.raises(RuntimeError, match="BACKEND_CORS_ORIGINS"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_falla_en_prod_si_el_wildcard_va_entre_otros_origenes(monkeypatch) -> None:
    """El '*' cuela igual aunque venga acompañado: la lista se valida entera, no solo si es '*'."""
    monkeypatch.setattr(main_module, "_IS_PROD", True)
    monkeypatch.setattr(
        main_module.settings, "SUPABASE_SERVICE_ROLE_KEY", "un-service-role-key-real-de-produccion"
    )
    monkeypatch.setattr(
        main_module.settings, "BACKEND_CORS_ORIGINS", "https://medicosporvenezuela.org,*"
    )

    with pytest.raises(RuntimeError, match="BACKEND_CORS_ORIGINS"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_ok_en_dev_aunque_cors_sea_wildcard(monkeypatch) -> None:
    """El guard es solo de produccion: en dev el '*' sigue siendo comodo y no rompe nada."""
    monkeypatch.setattr(main_module, "_IS_PROD", False)
    monkeypatch.setattr(main_module.settings, "BACKEND_CORS_ORIGINS", "*")

    async with main_module.lifespan(main_module.app):
        pass
