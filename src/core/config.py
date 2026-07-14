"""Configuración de la aplicación cargada desde variables de entorno.

Desarrollo: por defecto apunta a un Postgres LOCAL (servicio `db` de docker-compose).
Producción: se define `DATABASE_URL` (o las piezas POSTGRES_*) apuntando a Supabase.

El driver es asíncrono (asyncpg), así que la URL usa el esquema postgresql+asyncpg://.
"""

import ssl
from functools import lru_cache
from typing import Any
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

# Modos de SSL (asyncpg no entiende "sslmode" en la URL; el cifrado va por
# connect_args, ver db/session.py). Semántica de Postgres:
#   - require            -> cifra pero NO verifica la CA (verify_mode=CERT_NONE).
#   - verify-ca/-full    -> cifra Y verifica la CA (requiere la CA en el trust store).
# Supabase usa una CA self-signed: con verify-full falla ("self-signed certificate
# in certificate chain"); por eso 'require' debe cifrar sin verificar.
_SSL_NO_VERIFY = {"require"}
_SSL_VERIFY = {"verify-ca", "verify-full"}
_SSL_REQUIRED = _SSL_NO_VERIFY | _SSL_VERIFY


class Settings(BaseSettings):
    """Lee la configuración desde el entorno (o un archivo .env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Metadatos de la API ---
    PROJECT_NAME: str = "API Médicos por Venezuela"
    API_V1_PREFIX: str = "/api/v1"
    ENVIRONMENT: str = "development"

    # --- Base de datos ---
    # DATABASE_URL tiene prioridad. Si no se define, se arma desde las piezas POSTGRES_*.
    # Los valores por defecto apuntan al Postgres local de docker-compose.
    DATABASE_URL: str | None = None
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "medicos"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "localdev"
    POSTGRES_SSLMODE: str = "prefer"

    # Pooler en modo transaction (Supabase, puerto 6543) requiere desactivar el
    # caché de prepared statements de asyncpg. Es inocuo en local.
    DB_DISABLE_PREPARED_STATEMENTS: bool = True

    # --- Autenticación (JWT de Supabase) ---
    # Secreto JWT del proyecto Supabase (Project Settings -> API -> JWT Secret).
    # En producción es OBLIGATORIO definirlo por entorno. El valor por defecto es solo
    # para desarrollo/pruebas locales y NO debe usarse en producción.
    SUPABASE_JWT_SECRET: str = "dev-insecure-jwt-secret-change-me"
    SUPABASE_JWT_ALGORITHM: str = "HS256"
    SUPABASE_JWT_AUDIENCE: str = "authenticated"
    # Opcional: URL de JWKS (claves asimétricas ES256/RS256 "JWT signing keys" de Supabase;
    # el CLI de Supabase local las usa por defecto: {API_URL}/auth/v1/.well-known/jwks.json).
    # Si no se define, solo se valida HS256 con SUPABASE_JWT_SECRET (comportamiento de siempre).
    SUPABASE_JWKS_URL: str | None = None

    # --- Supabase Admin API (creación de usuarios de Auth) ---
    # Base URL del proyecto Supabase (local: el gateway del CLI; prod: el proyecto real).
    # El service-role key da acceso admin total (bypassa RLS): NUNCA se loguea y solo lo
    # usa `src/services/users.py`. En producción es OBLIGATORIO definirlo por entorno; el
    # valor por defecto es solo para desarrollo/pruebas locales.
    SUPABASE_URL: str = "http://127.0.0.1:54321"
    SUPABASE_SERVICE_ROLE_KEY: str = "dev-insecure-service-role-key-change-me"

    # --- Resiliencia de la cola ---
    # Minutos tras los cuales una consulta 'in_progress' sin cerrar se considera
    # estancada y se devuelve a 'waiting' (la libera para otro médico).
    STALE_CONSULTATION_MINUTES: int = 30

    # --- Videoconsulta (Jitsi) ---
    # Instancia self-hosted (salas abiertas, sin moderador). NO se usa el público meet.jit.si por
    # defecto porque ahora exige login de moderador ("no moderators have yet arrived"). Override
    # con la env JITSI_DOMAIN si el host cambia.
    JITSI_DOMAIN: str = "meet.medicosporvenezuela.org"

    # --- CORS ---
    BACKEND_CORS_ORIGINS: str = "*"

    # --- Anti-abuso (rate limiting) ---
    # Storage en memoria por proceso; para varias instancias, usar Redis.
    RATE_LIMIT_ENABLED: bool = True
    DOCTOR_REGISTER_RATE_LIMIT: str = "5/minute"

    def _normalize_async_scheme(self, url: str) -> str:
        """Garantiza el driver async (postgresql+asyncpg://)."""
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql+psycopg2://"):
            return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    @property
    def sqlalchemy_database_uri(self) -> str:
        """URL de conexión async para SQLAlchemy (sin parámetros de SSL)."""
        if self.DATABASE_URL:
            # Quita un eventual ?sslmode=... que asyncpg no entiende.
            base = self._normalize_async_scheme(self.DATABASE_URL)
            return base.split("?", 1)[0]
        user = quote_plus(self.POSTGRES_USER)
        password = quote_plus(self.POSTGRES_PASSWORD)
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def ssl_required(self) -> bool:
        return self.POSTGRES_SSLMODE.lower() in _SSL_REQUIRED

    @property
    def connect_args(self) -> dict[str, Any]:
        """Argumentos de conexión para asyncpg."""
        args: dict[str, Any] = {}
        mode = self.POSTGRES_SSLMODE.lower()
        if mode in _SSL_VERIFY:
            # Verifica la CA (necesita la CA en el trust store; p. ej. la de Supabase).
            args["ssl"] = True
        elif mode in _SSL_NO_VERIFY:
            # require: cifra pero NO verifica la CA (asyncpg ssl=True verificaría =
            # verify-full, que rompe con la CA self-signed del pooler de Supabase).
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            args["ssl"] = ctx
        if self.DB_DISABLE_PREPARED_STATEMENTS:
            # Necesario detrás de PgBouncer en modo transaction (Supabase 6543).
            args["statement_cache_size"] = 0
            args["prepared_statement_cache_size"] = 0
        return args

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.BACKEND_CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
