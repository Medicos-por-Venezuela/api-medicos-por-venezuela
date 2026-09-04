"""Envío de correos (Mailtrap) — la base para recordatorios, alertas y difusiones.

Diseño:
- Best-effort: un fallo de correo NUNCA rompe el flujo que lo dispara — se loguea
  (categoría + tipo de error, jamás PII ni contenido) y se devuelve False.
- Apagado sin token: sin MAILTRAP_API_TOKEN el servicio es un no-op con warning,
  así local/tests no envían nada por accidente.
- El SDK de Mailtrap es síncrono (requests): el send corre en un hilo
  (asyncio.to_thread) para no bloquear el event loop.
- `category` es la categoría de Mailtrap (analytics/filtros): usa valores estables
  tipo "recordatorio" o "alerta".
- Dos streams: el TRANSACCIONAL (default) para correos individuales, donde importa la
  prioridad de entrega; y el BULK (`bulk=True` -> bulk.api.mailtrap.io) para difusiones a
  muchos destinatarios. Ver `send_bulk`.
"""

import asyncio
import html
import logging

import mailtrap as mt

from src.core.config import settings

logger = logging.getLogger("mpv.api")


def esc(value: object) -> str:
    """Escapa un valor para incrustarlo en el cuerpo HTML de un correo.

    NO es decorativo. Casi todo lo que acaba en estos correos —el nombre de un paciente, su
    zona, el motivo de una referencia— lo teclea alguien: unos vienen de formularios PÚBLICOS
    y sin autenticar (`POST /patients`, `POST /doctors`), otros los escribe un médico en el
    panel. Sin escapar, un dato con `<a href="http://malo/">Aprobar</a>` dentro le mete a quien
    lo lea un enlace vivo, con apariencia de venir de la plataforma, dentro de un correo que la
    plataforma sí envió: phishing servido por nosotros. Da igual que el destinatario sea la
    bandeja de operación, un médico o un paciente; el vector es el mismo.

    Vive aquí y no en cada módulo de composición porque el criterio es uno solo y los
    constructores de cuerpos ya importan de este fichero. El cuerpo de texto plano no lo
    necesita; el HTML sí.
    """
    return html.escape(str(value), quote=True)


def mail_enabled() -> bool:
    """True si hay MAILTRAP_API_TOKEN configurado (el envío está habilitado)."""
    return bool(settings.MAILTRAP_API_TOKEN)


def _sandbox_client() -> mt.MailtrapClient | None:
    """Cliente de sandbox (Email Testing) si hay inbox configurado; None si no.

    Tiene prioridad sobre el stream bulk: local y tests entregan al inbox de pruebas aunque
    el caller pida difusión."""
    if not settings.MAILTRAP_INBOX_ID:
        return None
    return mt.MailtrapClient(
        token=settings.MAILTRAP_API_TOKEN,
        sandbox=True,
        inbox_id=settings.MAILTRAP_INBOX_ID,
    )


def _client() -> mt.MailtrapClient:
    """Stream TRANSACCIONAL: correos individuales, donde importa la prioridad de entrega."""
    return _sandbox_client() or mt.MailtrapClient(token=settings.MAILTRAP_API_TOKEN)


def _bulk_client() -> mt.MailtrapClient:
    """Stream BULK (bulk.api.mailtrap.io): difusiones a muchos destinatarios.

    Factoría aparte y no un parámetro de `_client()`: los tests doblan estas funciones, y
    cambiarle la firma a `_client` rompería los dobles ya escritos para el flujo individual."""
    return _sandbox_client() or mt.MailtrapClient(token=settings.MAILTRAP_API_TOKEN, bulk=True)


async def send_mail(
    to_email: str,
    subject: str,
    text: str,
    html: str | None = None,
    category: str | None = None,
    bcc: list[str] | None = None,
    bulk: bool = False,
) -> bool:
    """Envía un correo. True si Mailtrap lo aceptó; False si está deshabilitado o falló.

    No lances desde aquí ni asumas éxito: los correos son best-effort (un recordatorio
    que no sale no puede tumbar el cierre de una consulta ni un registro).

    `bcc` sirve para difundir el MISMO mensaje sin que los destinatarios se vean entre sí
    (ver `send_bulk`); `bulk` lo manda por el stream de alto volumen.
    """
    if not mail_enabled():
        logger.warning("MAIL:disabled category=%s (sin MAILTRAP_API_TOKEN)", category)
        return False

    try:
        # El armado va DENTRO del try, no antes. `mt.Address` valida el formato y lanza si la
        # dirección no le cuadra: con la construcción fuera, una dirección mal escrita en la
        # configuración (una coma en vez de un punto en MAIL_INTERNAL_RECIPIENTS, p. ej.)
        # reventaba hacia el caller y rompía justo el flujo que este módulo promete no romper.
        mail = mt.Mail(
            sender=mt.Address(email=settings.MAIL_FROM_EMAIL, name=settings.MAIL_FROM_NAME),
            to=[mt.Address(email=to_email)],
            bcc=[mt.Address(email=e) for e in bcc] if bcc else None,
            subject=subject,
            text=text,
            html=html,
            category=category,
        )
        await asyncio.to_thread((_bulk_client() if bulk else _client()).send, mail)
    except Exception as exc:  # noqa: BLE001 — best-effort: nada de correo revienta al caller
        logger.warning("MAIL:failed category=%s reason=%s", category, type(exc).__name__)
        return False
    logger.info("MAIL:sent category=%s", category)
    return True


async def send_bulk(
    recipients: list[str],
    subject: str,
    text: str,
    html: str | None = None,
    category: str | None = None,
) -> int:
    """Difunde el MISMO correo a muchos destinatarios. Devuelve a cuántos se les envió.

    Por qué así y no un `send_mail` por cabeza: una especialidad puede tener cientos de
    médicos, y el SDK es síncrono — serían cientos de peticiones secuenciales, minutos de
    espera y un candidato seguro al rate limit. En lotes de `MAIL_BULK_BATCH_SIZE` por el
    stream bulk, 300 médicos son 6 peticiones.

    **BCC y no `to` múltiple**: los correos de los médicos son datos de colegas y no deben
    quedar expuestos en la cabecera del resto. El `to` visible es la propia plataforma.

    Best-effort igual que `send_mail`: si un lote falla, los demás siguen y la cuenta refleja
    solo lo que salió. Nunca lanza.
    """
    if not mail_enabled():
        logger.warning("MAIL:disabled category=%s (sin MAILTRAP_API_TOKEN)", category)
        return 0

    # Sin duplicados y preservando el orden (un médico con dos vías de contacto igual recibe uno).
    unicos = list(dict.fromkeys(e for e in recipients if e))
    if len(unicos) > settings.MAIL_FANOUT_MAX:
        # Nunca truncar en silencio: sin este log, "se notificó a 500" se leería como "a todos".
        logger.warning(
            "MAIL:fanout_truncated category=%s destinatarios=%d tope=%d",
            category,
            len(unicos),
            settings.MAIL_FANOUT_MAX,
        )
        unicos = unicos[: settings.MAIL_FANOUT_MAX]

    tamano = max(1, settings.MAIL_BULK_BATCH_SIZE)
    enviados = 0
    for inicio in range(0, len(unicos), tamano):
        lote = unicos[inicio : inicio + tamano]
        if await send_mail(
            settings.MAIL_FROM_EMAIL,
            subject,
            text,
            html=html,
            category=category,
            bcc=lote,
            bulk=True,
        ):
            enviados += len(lote)
    logger.info("MAIL:bulk category=%s enviados=%d de=%d", category, enviados, len(unicos))
    return enviados
