"""Pruebas unitarias de la construcción de configuración (URL async y SSL)."""

import ssl

from src.core.config import Settings


def test_uri_from_parts_is_async() -> None:
    s = Settings(
        DATABASE_URL=None,
        POSTGRES_HOST="localhost",
        POSTGRES_PORT=5432,
        POSTGRES_DB="medicos",
        POSTGRES_USER="postgres",
        POSTGRES_PASSWORD="p@ss word",
        POSTGRES_SSLMODE="prefer",
    )
    uri = s.sqlalchemy_database_uri
    assert uri.startswith("postgresql+asyncpg://")
    assert "p%40ss+word" in uri or "p%40ss%20word" in uri  # password URL-encoded
    assert "sslmode" not in uri  # asyncpg no usa sslmode en la URL


def test_database_url_is_normalized_and_stripped() -> None:
    s = Settings(
        DATABASE_URL="postgresql://u:p@host:6543/postgres?sslmode=require",
    )
    uri = s.sqlalchemy_database_uri
    assert uri == "postgresql+asyncpg://u:p@host:6543/postgres"


def test_connect_args_require_cifra_sin_verificar() -> None:
    # require: cifra pero NO verifica la CA (Supabase usa CA self-signed).
    s = Settings(POSTGRES_SSLMODE="require", DB_DISABLE_PREPARED_STATEMENTS=True)
    args = s.connect_args
    assert isinstance(args["ssl"], ssl.SSLContext)
    assert args["ssl"].verify_mode == ssl.CERT_NONE
    assert args["ssl"].check_hostname is False
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0

    s2 = Settings(POSTGRES_SSLMODE="disable", DB_DISABLE_PREPARED_STATEMENTS=False)
    assert s2.connect_args == {}
    assert s2.ssl_required is False


def test_connect_args_verify_full_verifica_ca() -> None:
    # verify-full: verificación completa (ssl=True; requiere CA en el trust store).
    s = Settings(POSTGRES_SSLMODE="verify-full")
    assert s.connect_args["ssl"] is True
    assert s.ssl_required is True


def test_cors_origins_parsing() -> None:
    s = Settings(BACKEND_CORS_ORIGINS="https://a.com, https://b.com ,")
    assert s.cors_origins == ["https://a.com", "https://b.com"]


def test_cors_origins_strips_trailing_slash() -> None:
    # El header Origin del navegador nunca trae barra final; una barra en la env rompía el
    # match exacto de CORS. El parser la tolera (y descarta entradas que quedan vacías).
    s = Settings(BACKEND_CORS_ORIGINS="https://a.com/, https://b.com , /")
    assert s.cors_origins == ["https://a.com", "https://b.com"]
