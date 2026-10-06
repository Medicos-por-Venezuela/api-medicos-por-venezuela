"""Servicio de almacenamiento para adjuntos de mensajería (bucket chat-attachments).

Almacena los binarios bajo `consultations/{consultation_id}/attachments/{attachment_id}.bin`.
En local y entornos de prueba, utiliza un directorio local seguro.
"""

import os
from pathlib import Path

from src.core.config import settings

_BASE_STORAGE_PATH = Path("/tmp/medico-storage") / settings.STORAGE_BUCKET_ATTACHMENTS


def _get_absolute_path(storage_path: str) -> Path:
    # Sanitizar contra path traversal
    normalized = os.path.normpath(storage_path).lstrip("/")
    return _BASE_STORAGE_PATH / normalized


def save_attachment_file(storage_path: str, content: bytes) -> str:
    """Guarda un archivo binario en el almacenamiento y devuelve la ruta relativa."""
    dest = _get_absolute_path(storage_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return storage_path


def get_attachment_file(storage_path: str) -> bytes | None:
    """Lee un archivo binario del almacenamiento o devuelve None si no existe."""
    target = _get_absolute_path(storage_path)
    if not target.is_file():
        return None
    return target.read_bytes()


def delete_attachment_file(storage_path: str) -> bool:
    """Elimina un archivo si existe."""
    target = _get_absolute_path(storage_path)
    if target.is_file():
        target.unlink()
        return True
    return False
