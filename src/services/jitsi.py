"""Generación de URLs de salas Jitsi (portado de lib/jitsi.ts)."""

import uuid

from src.core.config import settings


def new_room_url() -> str:
    """Devuelve una URL de sala única: https://{JITSI_DOMAIN}/vamed-{uuid}."""
    return f"https://{settings.JITSI_DOMAIN}/vamed-{uuid.uuid4()}"
