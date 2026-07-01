"""Verifica el registro automático de routers (src/routers/__init__.py).

Garantiza que el auto-discovery realmente monta los endpoints esperados, para que
un router nuevo no quede silenciosamente sin registrar (ni uno existente desaparezca).
"""

from src.main import app
from src.routers import api_router, tags_metadata

# Prefijos que deben existir siempre (cada uno corresponde a un router del paquete).
_EXPECTED_PREFIXES = {
    "/auth",
    "/queue",
    "/consultations",
    "/patients",
    "/doctors",
    "/profiles",
    "/specialties",
}


def _registered_prefixes() -> set[str]:
    prefixes: set[str] = set()
    routes = []
    for route in api_router.routes:
        routes.extend(getattr(getattr(route, "original_router", None), "routes", [route]))
    for route in routes:
        path = getattr(route, "path", "")
        # Toma el primer segmento del path como prefijo del recurso.
        parts = path.strip("/").split("/")
        if parts and parts[0]:
            prefixes.add(f"/{parts[0]}")
    return prefixes


def test_auto_discovery_registra_todos_los_routers():
    registrados = _registered_prefixes()
    faltantes = _EXPECTED_PREFIXES - registrados
    assert not faltantes, f"Routers no registrados por el auto-discovery: {faltantes}"


def test_cada_tag_esperado_tiene_metadata():
    nombres = {t["name"] for t in tags_metadata}
    esperados = {p.strip("/") for p in _EXPECTED_PREFIXES}
    faltantes = esperados - nombres
    assert not faltantes, f"Tags sin descripción en Swagger: {faltantes}"


def test_app_incluye_tag_health_y_los_recolectados():
    nombres = {t["name"] for t in app.openapi_tags}
    assert "health" in nombres
    assert "auth" in nombres and "queue" in nombres
