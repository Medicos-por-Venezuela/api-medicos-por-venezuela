"""Envío de correos (Mailtrap) — la base para recordatorios y alertas.

Diseño:
- Best-effort: un fallo de correo NUNCA rompe el flujo que lo dispara — se loguea
  (categoría + tipo de error, jamás PII ni contenido) y se devuelve False.
- Apagado sin token: sin MAILTRAP_API_TOKEN el servicio es un no-op con warning,
  así local/tests no envían nada por accidente.
- El SDK de Mailtrap es síncrono (requests): el send corre en un hilo
  (asyncio.to_thread) para no bloquear el event loop.
- `category` es la categoría de Mailtrap (analytics/filtros): usa valores estables
  tipo "recordatorio" o "alerta".
"""

import asyncio
import logging

import mailtrap as mt

from src.core.config import settings

logger = logging.getLogger("mpv.api")


def mail_enabled() -> bool:
    """True si hay MAILTRAP_API_TOKEN configurado (el envío está habilitado)."""
    return bool(settings.MAILTRAP_API_TOKEN)


def _client() -> mt.MailtrapClient:
    if settings.MAILTRAP_INBOX_ID:
        # Sandbox (Email Testing): entrega al inbox de prueba, no a destinatarios reales.
        return mt.MailtrapClient(
            token=settings.MAILTRAP_API_TOKEN,
            sandbox=True,
            inbox_id=settings.MAILTRAP_INBOX_ID,
        )
    return mt.MailtrapClient(token=settings.MAILTRAP_API_TOKEN)


async def send_mail(
    to_email: str,
    subject: str,
    text: str,
    html: str | None = None,
    category: str | None = None,
) -> bool:
    """Envía un correo. True si Mailtrap lo aceptó; False si está deshabilitado o falló.

    No lances desde aquí ni asumas éxito: los correos son best-effort (un recordatorio
    que no sale no puede tumbar el cierre de una consulta ni un registro).
    """
    if not mail_enabled():
        logger.warning("MAIL:disabled category=%s (sin MAILTRAP_API_TOKEN)", category)
        return False

    mail = mt.Mail(
        sender=mt.Address(email=settings.MAIL_FROM_EMAIL, name=settings.MAIL_FROM_NAME),
        to=[mt.Address(email=to_email)],
        subject=subject,
        text=text,
        html=html,
        category=category,
    )
    try:
        await asyncio.to_thread(_client().send, mail)
    except Exception as exc:  # noqa: BLE001 — best-effort: nada de correo revienta al caller
        logger.warning("MAIL:failed category=%s reason=%s", category, type(exc).__name__)
        return False
    logger.info("MAIL:sent category=%s", category)
    return True
