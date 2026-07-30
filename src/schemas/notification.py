"""Esquemas de las preferencias de notificación del usuario."""

from pydantic import BaseModel


class NotificationPrefsResponse(BaseModel):
    """Preferencias del usuario + el catálogo de eventos/canales (para que la UI sepa qué mostrar
    sin duplicar el catálogo). `prefs`: { evento: {push, email} } (ausente = habilitado)."""

    prefs: dict
    catalog: dict[str, list[str]]  # { evento: [canales aplicables] }


class NotificationPrefsUpdate(BaseModel):
    """Cuerpo para guardar preferencias. Se sanea contra el catálogo en el servicio."""

    prefs: dict
