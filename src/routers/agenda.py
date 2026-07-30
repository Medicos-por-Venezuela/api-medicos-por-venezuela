"""Feed iCalendar de la agenda: URL de suscripción (webcal/ics) + el .ics público por token.

El .ics NO usa el JWT (los calendarios de Google/Apple sondean la URL sin él): autentica por el
token secreto de la URL (uuid no adivinable, solo lectura, regenerable). Ver services/calendar.py.
"""

import uuid

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.security import Principal, get_current_principal
from src.db.session import get_db
from src.schemas.agenda import CalendarUrlResponse
from src.services import calendar as calendar_service

router = APIRouter(prefix="/agenda", tags=["agenda"])
tag_metadata = [
    {"name": "agenda", "description": "Agenda y su feed iCalendar (sincronización de calendario)."}
]


def _urls(request: Request, token: uuid.UUID) -> CalendarUrlResponse:
    # base_url = raíz del host del request (en prod detrás de proxy, uvicorn --proxy-headers hace
    # que sea el host público). El feed vive bajo el prefijo de la API.
    base = str(request.base_url).rstrip("/")
    ics_url = f"{base}{settings.API_V1_PREFIX}/agenda/{token}.ics"
    webcal_url = ics_url.replace("https://", "webcal://").replace("http://", "webcal://")
    return CalendarUrlResponse(ics_url=ics_url, webcal_url=webcal_url)


@router.get(
    "/calendar-url",
    response_model=CalendarUrlResponse,
    summary="URL de suscripción del calendario (webcal/ics) del usuario",
)
async def calendar_url(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> CalendarUrlResponse:
    """URL personal para suscribir la agenda en Google/Apple/Outlook. Genera el token la 1ª vez.
    Sirve para médicos (sus citas asignadas) y pacientes (las suyas)."""
    token = await calendar_service.get_or_create_token(db, principal.id)
    return _urls(request, token)


@router.post(
    "/calendar-url/rotate",
    response_model=CalendarUrlResponse,
    summary="Regenerar el token del calendario (revoca la URL anterior)",
)
async def rotate_calendar_url(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> CalendarUrlResponse:
    """Rota el token: la URL anterior deja de funcionar (útil si se filtró)."""
    token = await calendar_service.rotate_token(db, principal.id)
    return _urls(request, token)


@router.get(
    "/{token}.ics",
    summary="Feed iCalendar de la agenda (público, autenticado por token secreto)",
)
async def agenda_ics(token: str, db: AsyncSession = Depends(get_db)) -> Response:
    """Devuelve el .ics con las citas del usuario dueño del token. 404 si el token no existe/parsea
    (sin filtrar si es inválido vs inexistente)."""
    try:
        tok = uuid.UUID(token)
    except ValueError:
        return Response(status_code=404)
    user = await calendar_service.user_by_token(db, tok)
    if user is None:
        return Response(status_code=404)
    ics = await calendar_service.agenda_ics_for_user(db, user)
    return Response(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="agenda.ics"'},
    )
