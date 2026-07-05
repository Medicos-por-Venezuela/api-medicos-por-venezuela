"""WebSocket de real-time para la cola (SOLO local).

En prod el frontend usa Supabase Realtime; este WS es el equivalente para el entorno
local (backend), donde no hay sesión de Supabase. Escucha `pg_notify('consultations_changed')`
(ver la migración del trigger) sobre una conexión asyncpg dedicada y reenvía cada cambio al
cliente. Se cierra si `ENVIRONMENT=production`.
"""

import asyncio
import logging

import asyncpg
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.core.config import settings
from src.core.security import decode_token

logger = logging.getLogger("mpv.api")

router = APIRouter(prefix="/ws", tags=["ws"])
tag_metadata = [
    {
        "name": "ws",
        "description": "Real-time por WebSocket (solo local; en prod es Supabase Realtime).",
    }
]

_CHANNEL = "consultations_changed"
_POLICY_VIOLATION = 1008


async def _listen_connection() -> asyncpg.Connection:
    """Conexión asyncpg dedicada para LISTEN (no del pool de SQLAlchemy).

    LISTEN necesita una sesión persistente; NO funciona tras el transaction pooler (6543),
    pero en local la BD es Postgres directo, que es donde se usa este WS.
    """
    dsn = settings.sqlalchemy_database_uri.replace("postgresql+asyncpg://", "postgresql://", 1)
    kwargs: dict = {}
    ssl_arg = settings.connect_args.get("ssl")
    if ssl_arg is not None:
        kwargs["ssl"] = ssl_arg
    if settings.DB_DISABLE_PREPARED_STATEMENTS:
        kwargs["statement_cache_size"] = 0
    return await asyncpg.connect(dsn, **kwargs)


@router.websocket("/consultations")
async def ws_consultations(websocket: WebSocket) -> None:
    """Empuja los cambios de `consultations` al cliente. Auth por `?token=<JWT>`
    (los navegadores no pueden setear headers en WebSocket). Inactivo en producción."""
    if settings.ENVIRONMENT == "production":
        await websocket.close(code=_POLICY_VIOLATION)
        return

    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=_POLICY_VIOLATION)
        return
    try:
        decode_token(token)  # valida firma/exp/audiencia; lanza si es inválido
    except Exception:  # noqa: BLE001  (HTTPException u otra: en cualquier caso, rechazar)
        await websocket.close(code=_POLICY_VIOLATION)
        return

    await websocket.accept()
    conn = await _listen_connection()
    queue: asyncio.Queue[str] = asyncio.Queue()

    def _on_notify(_c: object, _pid: int, _channel: str, payload: str) -> None:
        queue.put_nowait(payload)

    await conn.add_listener(_CHANNEL, _on_notify)

    async def _forward() -> None:
        while True:
            payload = await queue.get()
            await websocket.send_text(payload)

    forwarder = asyncio.create_task(_forward())
    try:
        # receive() detecta la desconexión del cliente mientras _forward() envía.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        forwarder.cancel()
        try:
            await conn.remove_listener(_CHANNEL, _on_notify)
        finally:
            await conn.close()
