#!/usr/bin/env python
"""Cifra los datos clínicos históricos (y re-cifra tras rotar la clave).

Por qué es Python y no SQL: la clave vive SOLO en el entorno de la API. Un script SQL
(`pgp_sym_encrypt(..., 'clave')`) la dejaría en el texto de la consulta, en pg_stat_statements,
en los logs de Postgres y en el historial del SQL Editor de Supabase, que es justo donde no debe
estar. Este script usa el mismo módulo de cifrado que la API y habla con la base por asyncpg.

Qué cifra: todas las columnas del ORM declaradas con `EncryptedText` (una sola lista, la del
código). Por fila: si está en claro la cifra; si está cifrada con una clave que no es la activa
(rotación) la re-cifra; si ya está con la activa, no la toca. Idempotente: se puede cortar y
volver a correr.

Concurrencia: cada UPDATE es condicional (`WHERE id = $1 AND col = $valor_leído`). Si la API
cambió la fila entre la lectura y la escritura, no se pisa: se cuenta como "cambiada" y la
próxima corrida la encuentra ya cifrada por la API. Nunca imprime contenido clínico, solo
conteos.

Rollback de emergencia (`--decrypt`): deja las columnas otra vez en claro, para volver a una
versión de la API anterior al cifrado, que no sabe leer `enc:v1:`. Escribe datos clínicos en
claro en la base, así que exige `--yes`, y se niega si ya están los CHECK de
`db/post-backfill/` (los rechazarían fila a fila). Secuencia: desplegar la API vieja, correr
`--decrypt --yes` (lo que la API nueva escribió cifrado mientras tanto también se descifra) y
`--decrypt --verify`. Se puede repetir.

Auditoría: toda corrida que ESCRIBE (cifrar o `--decrypt --yes`) exige `--operator` (quién la
lanza) y deja en `audit_log` una fila `clinical_data.bulk_encrypt` / `clinical_data.bulk_decrypt`
ANTES de tocar datos (si no se puede escribir, no toca nada) y otra al terminar con los conteos
—o con el error, si falla a mitad—, unidas por el mismo `correlation_id`. El script corre fuera
de la API, así que sin esto un descifrado masivo no dejaría rastro. No frena a quien tenga la
clave y escriba su propio código: deja constancia de los usos legítimos.

Uso (con el mismo .env / DATABASE_URL que la API):
  uv run python scripts/encrypt_clinical_data.py --generate-key   # clave nueva, no toca la BD
  uv run python scripts/encrypt_clinical_data.py --dry-run        # cuenta lo pendiente
  uv run python scripts/encrypt_clinical_data.py --operator yo@x  # cifra
  uv run python scripts/encrypt_clinical_data.py --verify         # exit 1 si queda algo
  uv run python scripts/encrypt_clinical_data.py --vacuum         # VACUUM FULL de las tablas
  uv run python scripts/encrypt_clinical_data.py --decrypt --dry-run                # cuenta
  uv run python scripts/encrypt_clinical_data.py --decrypt --yes --operator yo@x    # ROLLBACK
  uv run python scripts/encrypt_clinical_data.py --decrypt --verify   # exit 1 si queda cifrado
En producción, desde la imagen desplegada (ver docs/cifrado-datos-clinicos.md):
  docker compose -f docker-compose.prod.yml run --rm api python scripts/encrypt_clinical_data.py
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import socket
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


@dataclass(frozen=True, order=True)
class Target:
    table: str
    column: str

    @property
    def field(self) -> str:
        return f"{self.table}.{self.column}"


def encrypted_targets() -> list[Target]:
    """Columnas `EncryptedText` registradas en el ORM, en orden estable."""
    import src.models  # noqa: F401  (registra todos los modelos en Base.metadata)
    from src.db.base import Base
    from src.db.encrypted import EncryptedText

    return sorted(
        Target(table.name, col.name)
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, EncryptedText)
    )


def _pending_sql(t: Target, *, decrypt: bool = False) -> str:
    """Filas por procesar. Cifrar: lo que no está con la clave activa (`not like $1`, con $1 =
    prefijo de la activa). Descifrar: todo lo cifrado (`like $1`, con $1 = `enc:v1:%`)."""
    # Identificadores del ORM, no del usuario: seguros de interpolar. Los valores van por $n.
    op = "like" if decrypt else "not like"
    return (
        f'select id, "{t.column}" as v from public."{t.table}" '
        f'where "{t.column}" is not null and "{t.column}" {op} $1 and id > $2 '
        f"order by id limit $3"
    )


@dataclass
class Result:
    done: int = 0
    raced: int = 0
    # Ids (no contenido) de filas que no descifran: cifradas con una clave que no está en el
    # llavero, manipuladas, o texto legado que casualmente empieza por `enc:v1:`. No abortan la
    # corrida: se informan para revisarlas a mano y `--verify` las sigue contando.
    unreadable: list[str] = field(default_factory=list)


async def process(
    conn, t: Target, *, batch_size: int, dry_run: bool, decrypt: bool = False
) -> Result:
    from src.core.clinical_crypto import PREFIX, ClinicalCryptoError, is_ciphertext, keyring

    ring = keyring()
    pattern = f"{PREFIX}%" if decrypt else f"{PREFIX}{ring.active_kid}:%"
    result = Result()
    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        rows = await conn.fetch(_pending_sql(t, decrypt=decrypt), pattern, last_id, batch_size)
        if not rows:
            return result
        last_id = rows[-1]["id"]
        if dry_run:
            result.done += len(rows)
            continue
        ids, olds, news = [], [], []
        for row in rows:
            old = row["v"]
            try:
                plain = ring.decrypt(old, field=t.field) if is_ciphertext(old) else old
            except ClinicalCryptoError:
                result.unreadable.append(str(row["id"]))
                continue
            ids.append(row["id"])
            olds.append(old)
            news.append(plain if decrypt else ring.encrypt(plain, field=t.field))
        if not ids:
            continue
        # Un UPDATE por lote, no por fila: fila a fila eran ~46 ms cada una en el ensayo con la
        # copia de prod (3.528 valores en 2 min 44 s), casi todo espera de red. La condición
        # `col = v.old` se conserva por fila: lo que la API cambió entre lectura y escritura no
        # se pisa y cuenta como `raced`.
        updated = await conn.fetch(
            f'update public."{t.table}" as x set "{t.column}" = v.new '
            f"from unnest($1::uuid[], $2::text[], $3::text[]) as v(id, old, new) "
            f'where x.id = v.id and x."{t.column}" = v.old returning x.id',
            ids,
            olds,
            news,
        )
        result.done += len(updated)
        result.raced += len(ids) - len(updated)


async def audit_run(
    conn, *, run_id: str, action: str, operator: str, phase: str, detail: dict
) -> None:
    """Una fila en `audit_log` (append-only). Si el operador es el correo de una cuenta, queda
    también como `actor_user_id` para cruzarlo con el resto de su actividad."""
    actor = await conn.fetchval(
        "select id from public.users where lower(email) = lower($1) limit 1", operator
    )
    metadata = {
        "phase": phase,
        "operator": operator,
        # Dentro de un contenedor son el usuario del contenedor y su id: poco útiles solos,
        # pero distinguen una corrida desde el EC2 de una desde el portátil de alguien.
        "os_user": getpass.getuser(),
        "host": socket.gethostname(),
        **detail,
    }
    await conn.execute(
        "insert into public.audit_log (actor_user_id, action, resource, metadata, correlation_id) "
        "values ($1, $2, 'clinical_data', $3::jsonb, $4)",
        actor,
        action,
        json.dumps(metadata),
        run_id,
    )


async def vacuum(conn) -> None:
    """`VACUUM (FULL, ANALYZE)` de las tablas con columnas cifradas: un UPDATE deja la versión
    vieja de la fila (en claro, antes del backfill) en disco hasta que se reescribe la tabla.
    Toma un lock exclusivo por tabla; con el volumen actual son milisegundos."""
    for table in sorted({t.table for t in encrypted_targets()}):
        await conn.execute(f'vacuum (full, analyze) public."{table}"')
        print(f"  VACUUM FULL {table}: OK")


async def run(args: argparse.Namespace) -> int:
    import asyncpg

    from scripts.migrate import _connect_kwargs
    from src.core.clinical_crypto import keyring

    kid = keyring().active_kid
    if (error := key_guard(kid, args.expect_kid)) is not None:
        print(error, file=sys.stderr)
        return 2
    if args.decrypt and not (args.yes or args.dry_run or args.verify):
        print(
            "--decrypt escribe los datos clínicos EN CLARO en la base. Es un rollback de "
            "emergencia: repite con --yes si es lo que quieres.",
            file=sys.stderr,
        )
        return 2
    writes = not (args.dry_run or args.verify or args.vacuum)
    operator = (args.operator or "").strip()
    if writes and len(operator) < 3:
        print(
            "Esta corrida escribe datos clínicos: indica quién la lanza con --operator "
            "(tu correo). Queda en audit_log.",
            file=sys.stderr,
        )
        return 2
    targets = encrypted_targets()
    print(f"Clave activa: {kid}. Columnas cifradas: {len(targets)}.")
    conn = await asyncpg.connect(**_connect_kwargs())
    if args.vacuum:
        try:
            await vacuum(conn)
        finally:
            await conn.close()
        return 0
    total = 0
    unreadable = 0
    counts: dict[str, int] = {}
    raced_total = 0
    run_id = uuid.uuid4().hex
    action = "clinical_data.bulk_decrypt" if args.decrypt else "clinical_data.bulk_encrypt"
    try:
        if args.decrypt and not (args.dry_run or args.verify):
            checks = await conn.fetchval(
                r"select count(*) from pg_constraint where conname like '%\_cifrado'"
            )
            if checks:
                print(
                    f"Hay {checks} CHECK de texto cifrado (db/post-backfill/): rechazarían el "
                    "texto en claro. Quítalos antes (alter table … drop constraint …_cifrado).",
                    file=sys.stderr,
                )
                return 2
        if writes:
            # Antes de tocar nada: si el audit no se puede escribir, la corrida no empieza.
            await audit_run(
                conn,
                run_id=run_id,
                action=action,
                operator=operator,
                phase="started",
                detail={"kid": kid},
            )
            print(f"Corrida auditada: {action} (correlation_id {run_id}).")
        for t in targets:
            r = await process(
                conn,
                t,
                batch_size=args.batch_size,
                dry_run=args.dry_run or args.verify,
                decrypt=args.decrypt,
            )
            total += r.done
            unreadable += len(r.unreadable)
            raced_total += r.raced
            if r.done:
                counts[t.field] = r.done
            if r.done or r.raced:
                done_verb = "descifradas" if args.decrypt else "cifradas"
                verb = "pendientes" if (args.dry_run or args.verify) else done_verb
                extra = f", {r.raced} cambiadas por la API durante la corrida" if r.raced else ""
                print(f"  {t.field}: {r.done} {verb}{extra}")
            if r.unreadable:
                muestra = ", ".join(r.unreadable[:10])
                print(f"  {t.field}: {len(r.unreadable)} INDESCIFRABLES (ids: {muestra})")
        if writes:
            await audit_run(
                conn,
                run_id=run_id,
                action=action,
                operator=operator,
                phase="finished",
                detail={
                    "kid": kid,
                    "total": total,
                    "counts": counts,
                    "raced": raced_total,
                    "unreadable": unreadable,
                },
            )
    except Exception as exc:
        if writes:
            # Lo que alcanzó a procesarse ya está escrito, lote a lote: que conste hasta dónde.
            # Si la conexión es lo que falló, este audit también fallará: se avisa y se deja
            # subir el error original, que es el que el operador necesita ver.
            try:
                await audit_run(
                    conn,
                    run_id=run_id,
                    action=action,
                    operator=operator,
                    phase="failed",
                    detail={
                        "kid": kid,
                        "total": total,
                        "counts": counts,
                        "error": type(exc).__name__,
                    },
                )
            except Exception:
                print(
                    f"No se pudo auditar el fallo (correlation_id {run_id}): consta solo el "
                    "inicio de la corrida.",
                    file=sys.stderr,
                )
        raise
    finally:
        await conn.close()
    if args.verify:
        ok = "OK: nada cifrado." if args.decrypt else "OK: todo cifrado con la clave activa."
        print(ok if total == 0 else f"Pendientes: {total}.")
        return 0 if total == 0 else 1
    done_word = "descifrado" if args.decrypt else "cifrado"
    print(f"Total {'pendiente' if args.dry_run else done_word}: {total}.")
    return 1 if unreadable else 0


def key_guard(active_kid: str, expect_kid: str | None) -> str | None:
    """Motivo para NO correr, o None. La clave de desarrollo está en el repo: cifrar con ella una
    base remota deja los datos legibles para cualquiera y opacos para la API de producción, y un
    `--verify` desde la misma shell diría OK. Pasa si falta la env en la shell del operador."""
    from src.core.config import settings
    from src.main import _INSECURE_CLINICAL_KID

    if expect_kid is not None and expect_kid != active_kid:
        return f"La clave activa es {active_kid}, no {expect_kid}. ¿Env equivocada?"
    remote = bool(settings.DATABASE_URL) or settings.ENVIRONMENT == "production"
    if remote and active_kid == _INSECURE_CLINICAL_KID:
        return (
            "Se está usando la clave de DESARROLLO contra una base remota. Define "
            "CLINICAL_DATA_ENCRYPTION_KEY (la de producción) antes de correr el script."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Solo cuenta lo pendiente.")
    mode.add_argument("--verify", action="store_true", help="Exit 1 si queda algo pendiente.")
    mode.add_argument("--generate-key", action="store_true", help="Imprime una clave nueva.")
    mode.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM (FULL, ANALYZE) de las tablas cifradas (tras el backfill).",
    )
    parser.add_argument(
        "--decrypt",
        action="store_true",
        help="ROLLBACK: descifra y deja las columnas en claro (con --yes, --dry-run o --verify).",
    )
    parser.add_argument("--yes", action="store_true", help="Confirma --decrypt.")
    parser.add_argument(
        "--operator",
        help="Quién lanza la corrida (su correo). Obligatorio al cifrar o descifrar: va al "
        "audit_log.",
    )
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument(
        "--expect-kid",
        help="kid de la clave de producción (lo imprime la API al arrancar y este script); "
        "aborta si la clave cargada es otra.",
    )
    args = parser.parse_args(argv)
    if args.generate_key:
        from src.core.clinical_crypto import generate_key

        print(generate_key())
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
