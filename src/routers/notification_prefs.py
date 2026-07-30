"""Preferencias de notificación del usuario (Ajustes del perfil): qué eventos recibir y por qué
canal (push nativo / correo). Para que el sistema no sea invasivo. Ver services/notifications."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import Principal, get_current_principal
from src.db.session import get_db
from src.schemas.notification import NotificationPrefsResponse, NotificationPrefsUpdate
from src.services import notifications

router = APIRouter(prefix="/me/notification-preferences", tags=["notifications"])
tag_metadata = [
    {"name": "notifications", "description": "Preferencias de notificación del usuario."}
]


def _catalog() -> dict[str, list[str]]:
    return {event: list(channels) for event, channels in notifications.NOTIFICATION_EVENTS.items()}


@router.get("", response_model=NotificationPrefsResponse, summary="Mis preferencias")
async def get_my_prefs(
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> NotificationPrefsResponse:
    prefs = await notifications.get_prefs(db, principal.id)
    return NotificationPrefsResponse(prefs=prefs, catalog=_catalog())


@router.patch("", response_model=NotificationPrefsResponse, summary="Actualizar mis preferencias")
async def update_my_prefs(
    payload: NotificationPrefsUpdate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> NotificationPrefsResponse:
    prefs = await notifications.set_prefs(db, principal.id, payload.prefs)
    return NotificationPrefsResponse(prefs=prefs, catalog=_catalog())
