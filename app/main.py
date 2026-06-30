"""Punto de entrada de la API de Médicos por Venezuela."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.routers import api_router
from app.core.config import settings
from app.db.session import get_db

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["health"])
def root() -> dict[str, str]:
    return {"service": settings.PROJECT_NAME, "status": "ok"}


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Comprueba la conexión a la base de datos."""
    db = next(get_db())
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "up"}
    except Exception as exc:  # pragma: no cover - diagnóstico
        return {"status": "degraded", "database": "down", "detail": str(exc)}
    finally:
        db.close()
