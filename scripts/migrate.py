#!/usr/bin/env python
"""CLI de migraciones (estilo Laravel) con tracking en la tabla schema_migrations.

Multiplataforma (Windows / macOS / Linux): usa asyncpg y la MISMA configuración que
la app (src.core.config), conectándose por TCP. No necesita bash ni `docker exec`.

Comandos:
  new "<descripción>"   Crea db/migrations/<AAAAMMDD_HHMMSS>_<slug>.sql y muestra la ruta a editar.
  status                Lista las migraciones: [aplicada] / [pendiente].
  up                    Aplica las pendientes, en orden, cada una en una transacción (default).

Conexión: toma DATABASE_URL o las piezas POSTGRES_* del entorno / .env (igual que la app).
Para producción (Supabase), exporta DATABASE_URL antes de correr `up`.

Uso:
  uv run python scripts/migrate.py status
  uv run python scripts/migrate.py new "add phone to doctors"
  uv run python scripts/migrate.py up
  # sin uv:  .venv/bin/python scripts/migrate.py up      (Windows: .venv\\Scripts\\python.exe)
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "db" / "migrations"
# Permite `import src...` aunque el paquete no esté instalado en modo editable.
sys.path.insert(0, str(ROOT))

# Evita UnicodeEncodeError en consolas Windows con codepage no-UTF8 (cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

_STUB = """\
-- Migración: {desc}
-- Creada:    {ts}
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).

"""

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def _connect_kwargs() -> dict:
    """Parámetros de conexión de asyncpg derivados de la config de la app."""
    from src.core.config import settings

    if settings.DATABASE_URL:
        # asyncpg quiere postgresql://, no el esquema +asyncpg de SQLAlchemy.
        dsn = settings.sqlalchemy_database_uri.replace("postgresql+asyncpg://", "postgresql://", 1)
        kwargs: dict = {"dsn": dsn}
    else:
        kwargs = {
            "host": settings.POSTGRES_HOST,
            "port": settings.POSTGRES_PORT,
            "user": settings.POSTGRES_USER,
            "password": settings.POSTGRES_PASSWORD,
            "database": settings.POSTGRES_DB,
        }
    if settings.ssl_required:
        kwargs["ssl"] = True
    if settings.DB_DISABLE_PREPARED_STATEMENTS:
        # Necesario tras el pooler transaction de Supabase (PgBouncer, 6543).
        kwargs["statement_cache_size"] = 0
    return kwargs


def _migration_files() -> list[Path]:
    """Migraciones ordenadas por nombre (001_ antes que 20260630_...)."""
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _has_sql(sql: str) -> bool:
    """¿El archivo tiene algún statement real, o solo comentarios/espacios?

    asyncpg falla al ejecutar un query sin sentencias (p. ej. una migración recién
    creada con `new` y aún vacía); en ese caso solo la registramos.
    """
    return bool(re.sub(r"--[^\n]*", "", sql).strip())


async def _applied_set(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT filename FROM public.schema_migrations")
    return {row["filename"] for row in rows}


def cmd_new(args: list[str]) -> int:
    desc = " ".join(args).strip()
    if not desc:
        print('Uso: migrate.py new "<descripción>"', file=sys.stderr)
        return 1
    slug = re.sub(r"[^a-z0-9]+", "_", desc.lower()).strip("_")
    now = datetime.now()
    path = MIGRATIONS_DIR / f"{now:%Y%m%d_%H%M%S}_{slug}.sql"
    MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(_STUB.format(desc=desc, ts=f"{now:%Y-%m-%d %H:%M:%S}"), encoding="utf-8")
    print("[migrate] Migración creada. Edita este archivo:")
    print(f"  {path.relative_to(ROOT).as_posix()}")
    return 0


async def cmd_status() -> int:
    conn = await asyncpg.connect(**_connect_kwargs())
    try:
        await conn.execute(_CREATE_TABLE)
        applied = await _applied_set(conn)
    finally:
        await conn.close()

    files = _migration_files()
    print("Migraciones (db/migrations):")
    done = pending = 0
    for path in files:
        if path.name in applied:
            print(f"  [aplicada]  {path.name}")
            done += 1
        else:
            print(f"  [pendiente] {path.name}")
            pending += 1
    if not files:
        print("  (no hay migraciones en db/migrations/)")
    print(f"Total: {done} aplicadas, {pending} pendientes.")
    return 0


async def cmd_up() -> int:
    conn = await asyncpg.connect(**_connect_kwargs())
    applied_now = 0
    try:
        await conn.execute(_CREATE_TABLE)
        applied = await _applied_set(conn)
        for path in _migration_files():
            if path.name in applied:
                continue
            print(f"[migrate] Aplicando {path.name}...")
            sql = path.read_text(encoding="utf-8")
            # La migración + su registro en una sola transacción: si falla, no se
            # registra nada (asyncpg usa el protocolo simple para multi-statement).
            async with conn.transaction():
                if _has_sql(sql):
                    await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO public.schema_migrations (filename) VALUES ($1)", path.name
                )
            applied_now += 1
    finally:
        await conn.close()
    print(f"[migrate] OK. Migraciones nuevas aplicadas: {applied_now}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "up"
    if cmd == "new":
        return cmd_new(argv[1:])
    if cmd == "status":
        return asyncio.run(cmd_status())
    if cmd == "up":
        return asyncio.run(cmd_up())
    if cmd in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    print(f"[migrate] comando desconocido: {cmd} (usa: new | status | up)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
