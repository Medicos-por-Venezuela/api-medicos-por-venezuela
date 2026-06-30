"""Configuración de la aplicación cargada desde variables de entorno.

Desarrollo: por defecto apunta a un Postgres LOCAL (el servicio `db` de docker-compose).
Producción: se define `DATABASE_URL` (o las piezas POSTGRES_*) apuntando a Supabase.
"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- CORS ---
    # Lista separada por comas de orígenes permitidos. "*" permite todos.
    BACKEND_CORS_ORIGINS: str = "*"

    @property
    def sqlalchemy_database_uri(self) -> str:
        """Construye la URL de conexión para SQLAlchemy."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        password = quote_plus(self.POSTGRES_PASSWORD)
        user = quote_plus(self.POSTGRES_USER)
        return (
            f"postgresql+psycopg2://{user}:{password}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            f"?sslmode={self.POSTGRES_SSLMODE}"
        )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.BACKEND_CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
