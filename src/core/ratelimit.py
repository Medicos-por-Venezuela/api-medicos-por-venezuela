"""Rate limiting con slowapi. Limiter compartido por la app y los routers.

El storage por defecto es en memoria (por proceso): suficiente para una sola
instancia. Con varios workers/instancias hay que apuntar a Redis (storage_uri).
Se desactiva en tests con `limiter.enabled = False`.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from src.core.config import settings

limiter = Limiter(key_func=get_remote_address, enabled=settings.RATE_LIMIT_ENABLED)
