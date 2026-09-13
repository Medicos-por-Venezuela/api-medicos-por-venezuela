"""Cliente de SOLO LECTURA de la API v4 de Kit: las métricas de los envíos de las encuestas.

Las encuestas de marketing salen por correo masivo desde Kit, y lo que pasa entre el envío y la
respuesta —cuántos lo recibieron, lo abrieron o hicieron clic— solo lo sabe Kit. Este módulo lo
trae para que el panel muestre el embudo completo (ver `services/marketing_performance.py`).

Tres decisiones:

1. **Cada envío se asocia solo a su encuesta**, por el enlace que lleva: `/encuesta/<slug>` entre
   los clics del envío (`GET /broadcasts/{id}/clicks`). Si todavía nadie hizo clic, Kit no lista el
   enlace, así que se cae al nombre de la plantilla ("Especialistas", "Medicos Generales",
   "Agradecimientos Psicologos"). Un envío que no se asocia a ninguna encuesta no es de esta
   campaña y se ignora.
2. **Caché en memoria** (`KIT_STATS_CACHE_SECONDS`): cada carga serían 1 + N peticiones a Kit, y
   las métricas cambian despacio. Es por proceso: con varios workers cada uno tiene la suya, que
   solo cuesta alguna petición de más.
3. **Kit caído no tumba el panel.** Cualquier fallo se traduce en `KitUnavailableError`, y el panel
   sigue con los datos de la plataforma.

⚠️ La clave da acceso a la cuenta de Kit, incluida la lista de suscriptores: no se loguea nunca, ni
siquiera dentro de un mensaje de error. Y las URLs de los clics traen en la query el correo de un
destinatario (`?email=...`): de ellas solo se lee la ruta, y no salen de este módulo.
"""

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as day_start
from urllib.parse import urlsplit

import httpx

from src.core.config import settings
from src.core.tz import VET

logger = logging.getLogger(__name__)

SURVEY_LINK = re.compile(r"/encuesta/(psicologos|especialistas|medicos-generales)/?$")

# Palabra del nombre de la plantilla (en minúsculas y sin tildes) -> encuesta. Solo para los
# envíos que aún no tienen clics.
TEMPLATE_KEYWORDS = (
    ("psicolog", "psicologos"),
    ("especialist", "especialistas"),
    ("general", "medicos-generales"),
)

# Estados de Kit de un envío que ya salió o está saliendo. Un borrador o uno programado no tiene
# métricas que sumar.
SENT_STATUSES = {"sending", "completed"}

_TIMEOUT = 10.0
# Aunque se pida "actualizar", no se vuelve a Kit antes de esto: un clic repetido en el botón no
# puede convertirse en una ráfaga de peticiones contra el límite de la API.
_MIN_REFRESH_SECONDS = 30
# Solo para tests: `httpx.MockTransport` en vez de la red.
_transport: httpx.AsyncBaseTransport | None = None
_cache: tuple[float, "Snapshot"] | None = None


class KitNotConfiguredError(Exception):
    """No hay `KIT_API_KEY`."""


class KitUnavailableError(Exception):
    """Kit no respondió, rechazó la clave o devolvió algo que no se entiende."""


@dataclass(frozen=True)
class Broadcast:
    """Un envío de Kit asociado a una encuesta, con sus métricas."""

    id: int
    subject: str
    sent_at: datetime  # UTC
    survey: str
    recipients: int
    opened: int
    clicked: int  # personas que hicieron clic, no clics totales
    unsubscribed: int


@dataclass(frozen=True)
class Snapshot:
    broadcasts: list[Broadcast]
    fetched_at: datetime  # UTC


def clear_cache() -> None:
    global _cache
    _cache = None


async def survey_broadcasts(*, refresh: bool = False) -> Snapshot:
    """Los envíos de las encuestas con sus métricas: de la caché si está fresca, o de Kit.

    Raises:
        KitNotConfiguredError: no hay clave.
        KitUnavailableError: Kit falló. La caché anterior se conserva para el siguiente intento.
    """
    global _cache
    if not settings.KIT_API_KEY:
        raise KitNotConfiguredError
    if _cache is not None:
        age = time.monotonic() - _cache[0]
        max_age = _MIN_REFRESH_SECONDS if refresh else settings.KIT_STATS_CACHE_SECONDS
        if age < max_age:
            return _cache[1]
    snapshot = await _fetch()
    _cache = (time.monotonic(), snapshot)
    return snapshot


def survey_from_template(name: str) -> str | None:
    """La encuesta que nombra una plantilla de Kit, sin importar mayúsculas ni tildes."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return next((slug for word, slug in TEMPLATE_KEYWORDS if word in plain), None)


async def _fetch() -> Snapshot:
    since = datetime.combine(settings.MARKETING_CAMPAIGNS_SINCE, day_start.min, tzinfo=VET)
    try:
        async with httpx.AsyncClient(
            base_url=settings.KIT_API_BASE_URL,
            headers={"X-Kit-Api-Key": settings.KIT_API_KEY, "Accept": "application/json"},
            timeout=_TIMEOUT,
            transport=_transport,
        ) as client:
            # Un envío por fila y hasta 1000 por página: la campaña no va a tener tantos desde
            # MARKETING_CAMPAIGNS_SINCE, así que no se pagina.
            listing = await _get(
                client,
                "/broadcasts/stats",
                params={"sent_after": since.astimezone(UTC).isoformat(), "per_page": 1000},
            )
            sent = [
                b
                for b in listing["broadcasts"]
                if b.get("send_at") and b["stats"]["status"] in SENT_STATUSES
            ]
            surveys = await asyncio.gather(*(_survey_of(client, b["id"]) for b in sent))
            broadcasts = [
                _broadcast(b, survey) for b, survey in zip(sent, surveys, strict=True) if survey
            ]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        # Solo el tipo y, si lo hay, el código: el mensaje de httpx trae la URL y, en otros
        # errores, podría traer cabeceras.
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning(
            "Kit no devolvió las métricas de los envíos (%s %s)", type(exc).__name__, status
        )
        raise KitUnavailableError from exc
    return Snapshot(
        broadcasts=sorted(broadcasts, key=lambda b: b.sent_at), fetched_at=datetime.now(UTC)
    )


async def _get(client: httpx.AsyncClient, path: str, **kwargs) -> dict:
    resp = await client.get(path, **kwargs)
    resp.raise_for_status()
    return resp.json()


async def _survey_of(client: httpx.AsyncClient, broadcast_id: int) -> str | None:
    clicks = await _get(client, f"/broadcasts/{broadcast_id}/clicks", params={"per_page": 100})
    for click in clicks["broadcast"]["clicks"]:
        if match := SURVEY_LINK.search(urlsplit(click["url"]).path):
            return match.group(1)
    detail = await _get(client, f"/broadcasts/{broadcast_id}")
    template = detail["broadcast"].get("email_template") or {}
    return survey_from_template(template.get("name") or "")


def _broadcast(raw: dict, survey: str) -> Broadcast:
    stats = raw["stats"]
    recipients = int(stats["recipients"])
    return Broadcast(
        id=int(raw["id"]),
        subject=raw.get("subject") or "",
        sent_at=datetime.fromisoformat(raw["send_at"]),
        survey=survey,
        recipients=recipients,
        opened=int(stats["emails_opened"]),
        # Kit da la tasa (con dos decimales) y no el número de personas; es la misma cifra que
        # su panel enseña como "Clicked" (7,20 % de 1.598 = 115).
        clicked=round(recipients * float(stats["click_rate"]) / 100),
        unsubscribed=int(stats["unsubscribes"]),
    )
