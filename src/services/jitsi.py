"""Generación de URLs de salas Jitsi (portado de lib/jitsi.ts)."""

import uuid

from src.core.config import settings

# Config que Jitsi lee del fragmento de la URL para saltarse el interstitial móvil de
# "descarga la app / abre la app". Va el clásico `disableDeepLinking` y el nuevo anidado
# `deeplinking.disabled` porque las dos formas conviven según la versión de la instancia.
# Mismo valor que `browserRoomUrl` en `lib/jitsi.ts`: sin esto, un paciente que abre el enlace
# desde el móvil aterriza en una pantalla que le pide instalar una app, y ahí se pierde.
_SIN_DEEP_LINKING = "config.disableDeepLinking=true&config.deeplinking.disabled=true"


def new_room_url() -> str:
    """Devuelve una URL de sala única: https://{JITSI_DOMAIN}/vamed-{uuid}."""
    return f"https://{settings.JITSI_DOMAIN}/vamed-{uuid.uuid4()}"


def browser_room_url(url: str) -> str:
    """Prepara una sala para abrirla en el navegador. Espejo de `browserRoomUrl` (lib/jitsi.ts).

    Hace falta en el backend desde que el enlace de la sala viaja por correo: hasta ahora
    siempre lo abría el frontend, que aplicaba esto al hacer clic. Un enlace de correo se abre
    solo, sin que ningún JavaScript nuestro lo toque antes.

    Dos cosas, las mismas que la versión del frontend:
    1. Reescribe las salas antiguas guardadas en `meet.jit.si`, que hoy exige moderador con
       sesión iniciada, a nuestra instancia abierta. Se aplica al usar la URL y no al crearla,
       así que también arregla lo que ya está en la base.
    2. Añade la config para saltar el interstitial móvil de la app.
    """
    if not url:
        return url
    url = url.replace("https://meet.jit.si/", f"https://{settings.JITSI_DOMAIN}/")
    if "disableDeepLinking" in url:
        return url
    separador = "&" if "#" in url else "#"
    return f"{url}{separador}{_SIN_DEEP_LINKING}"
