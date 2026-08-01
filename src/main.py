"""Punto de entrada de la API de Médicos por Venezuela (FastAPI async).

Esta API expone, como backend dedicado, la lógica de negocio que hoy vive acoplada
en la app Next.js conectada directamente a Supabase. Documentación interactiva en
`/docs` (Swagger UI) y `/redoc` — deshabilitada en ENVIRONMENT=production.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from src.core.config import settings
from src.core.exceptions import register_exception_handlers
from src.core.observability import (
    CorrelationIdMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
)
from src.core.ratelimit import limiter
from src.db.session import AsyncSessionLocal
from src.routers import api_router, tags_metadata

configure_logging()
logger = logging.getLogger("mpv.api")

_IS_PROD = settings.ENVIRONMENT == "production"
_INSECURE_JWT_DEFAULT = "dev-insecure-jwt-secret-change-me"
_INSECURE_SERVICE_ROLE_DEFAULT = "dev-insecure-service-role-key-change-me"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Validaciones de seguridad al arrancar. Falla rápido si la config es insegura."""
    if _IS_PROD and settings.SUPABASE_JWT_SECRET == _INSECURE_JWT_DEFAULT:
        raise RuntimeError(
            "SUPABASE_JWT_SECRET tiene el valor por defecto inseguro. "
            "Configúralo en producción (Supabase → Settings → API → JWT Secret)."
        )
    if _IS_PROD and settings.SUPABASE_SERVICE_ROLE_KEY == _INSECURE_SERVICE_ROLE_DEFAULT:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY tiene el valor por defecto inseguro. "
            "Configúralo en producción (Supabase → Settings → API → service_role key)."
        )
    # Deja los orígenes CORS efectivos en los logs de arranque: cuando un front rebota por CORS,
    # se ve de una si su origen está (o no) en la lista, sin adivinar desde el .env.
    # El default de BACKEND_CORS_ORIGINS es "*" y el middleware va con allow_credentials=True:
    # si la env no se setea en prod, se aceptarían credenciales desde cualquier origen.
    if _IS_PROD and "*" in settings.cors_origins:
        raise RuntimeError(
            "BACKEND_CORS_ORIGINS no puede ser '*' en producción (se envían credenciales). "
            "Define la lista explícita de orígenes del frontend, separados por comas."
        )
    logger.info("CORS origins efectivos: %s", settings.cors_origins)
    logger.info(
        "Mail (Mailtrap): %s",
        "habilitado" if settings.MAILTRAP_API_TOKEN else "deshabilitado (sin MAILTRAP_API_TOKEN)",
    )
    yield


# Descripción (markdown) que se muestra en la cabecera de Swagger UI / ReDoc.
API_DESCRIPTION = """
Backend REST de **Médicos por Venezuela**: conecta pacientes con médicos voluntarios
para videoconsultas, gestionando la **cola (Board)** en tiempo real.

### Arquitectura (Service Layer / 3-tier)
- **Routers** (`src/routers`): capa HTTP delgada (inyectan la sesión, delegan, mapean errores).
- **Services** (`src/services`): lógica de negocio + consultas SQLAlchemy + bloqueos.
- **Schemas** (`src/schemas`): validación Pydantic v2 (`Create` / `Update` / `Response`).
- **Models** (`src/models`): ORM SQLAlchemy 2.0 (async, driver asyncpg).

### Concurrencia (crítica)
El endpoint `POST /queue/{id}/take` usa bloqueo pesimista de fallo rápido
(`with_for_update(nowait=True)`): el primer médico gana (`200`), el resto recibe
`409 Conflict`. Nunca hay doble asignación ni peticiones colgadas.

### Entornos
- **Local (dev):** PostgreSQL en Docker con los datos reales restaurados desde Supabase.
- **Producción:** Supabase (Postgres) vía variables de entorno.
"""

# Metadatos por grupo de endpoints (se ven como secciones en Swagger).
# Solo `health` se define aquí (sus endpoints viven en este archivo); las demás
# descripciones las aporta cada router vía `tag_metadata` y se auto-recolectan
# en `src.routers` — así un endpoint nuevo no obliga a editar este archivo.
OPENAPI_TAGS = [
    {"name": "health", "description": "Estado del servicio y de la base de datos."},
    *tags_metadata,
]

app = FastAPI(
    lifespan=lifespan,
    title=settings.PROJECT_NAME,
    description=API_DESCRIPTION,
    version="0.1.0",
    # En producción: schema JSON, Swagger y ReDoc completamente ocultos (OSINT / A05).
    openapi_url=None if _IS_PROD else f"{settings.API_V1_PREFIX}/openapi.json",
    docs_url=None if _IS_PROD else "/docs",
    redoc_url=None if _IS_PROD else "/redoc",
    openapi_tags=OPENAPI_TAGS,
    contact={
        "name": "Médicos por Venezuela",
        "url": "https://github.com/Medicos-por-Venezuela/api-medicos-por-venezuela",
    },
    license_info={"name": "MIT"},
)

app.add_middleware(SecurityHeadersMiddleware, is_production=_IS_PROD)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
)

register_exception_handlers(app)

# Rate limiting (slowapi): expone el limiter y traduce el exceso a HTTP 429.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["health"], summary="Ping del servicio")
async def root() -> dict[str, str]:
    return {"service": settings.PROJECT_NAME, "status": "ok"}


@app.get("/health", tags=["health"], summary="Healthcheck (incluye la base de datos)")
async def health() -> dict[str, str]:
    """Comprueba la conexión a la base de datos ejecutando `SELECT 1`."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "database": "up"}
    except Exception:  # pragma: no cover - diagnóstico
        # Logueamos internamente pero NO exponemos el str(exc) al cliente (podría
        # contener la cadena de conexión con credenciales).
        logger.error("Health check falló", exc_info=True)
        return {"status": "degraded", "database": "down"}


@app.get(
    f"{settings.API_V1_PREFIX}/health",
    tags=["health"],
    summary="Healthcheck (alias bajo el prefijo de la API)",
)
async def health_v1() -> dict[str, str]:
    return await health()
