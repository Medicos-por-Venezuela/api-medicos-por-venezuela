"""Routers de la API v1 — registro por descubrimiento automático.

Cada módulo de este paquete que exponga un `router` (APIRouter) se incluye solo.
Para añadir un endpoint nuevo basta con crear `src/routers/mi_modulo.py` con:

    router = APIRouter(prefix="/mi-recurso", tags=["mi-recurso"])
    tag_metadata = [{"name": "mi-recurso", "description": "..."}]  # opcional, para Swagger

**No** hay que editar este archivo ni `main.py`: así varios devs añaden endpoints
en paralelo sin tocar archivos compartidos (cero conflictos de merge).
"""

import importlib
import pkgutil

from fastapi import APIRouter

api_router = APIRouter()

# Descripciones de tags recolectadas de cada módulo (alimentan `openapi_tags`).
tags_metadata: list[dict] = []

# Orden alfabético estable: el orden de inclusión no afecta el ruteo (cada router
# tiene un prefix único), solo el orden de las secciones en Swagger.
for _module_info in sorted(_m.name for _m in pkgutil.iter_modules(__path__)):
    _module = importlib.import_module(f"{__name__}.{_module_info}")
    _router = getattr(_module, "router", None)
    if isinstance(_router, APIRouter):
        api_router.include_router(_router)
    _meta = getattr(_module, "tag_metadata", None)
    if _meta:
        tags_metadata.extend(_meta)

__all__ = ["api_router", "tags_metadata"]
