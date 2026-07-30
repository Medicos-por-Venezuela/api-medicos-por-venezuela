"""Feed iCalendar (RFC 5545) de la agenda: suscripción webcal:// + descarga .ics.

Un solo formato que consumen Google Calendar, Apple (iPhone/Mac), Outlook, etc. — así no hay que
integrar cada proveedor por separado. El feed se autentica por un token secreto por usuario
(`users.calendar_token`, uuid no adivinable, regenerable) porque los calendarios sondean la URL sin
el JWT de Supabase. Reusa `consultations.list_agenda` (médico → sus citas; paciente → las suyas).
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError
from src.models.profile import Profile
from src.services import consultations as consultations_service

EVENT_MINUTES = 30  # duración asumida de una cita (las citas no guardan hora de fin)
_PRODID = "-//Medicos por Venezuela//Agenda//ES"
_UID_DOMAIN = "medicosporvenezuela.org"


def _esc(text: str) -> str:
    """Escape de texto para iCal (RFC5545 §3.3.11): \\ ; , y saltos de línea."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Pliega a ≤75 octetos con CRLF + espacio (RFC5545 §3.1), sin cortar chars multibyte."""
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    parts: list[str] = []
    while len(b) > 75:
        cut = 75
        while cut > 0 and (b[cut] & 0xC0) == 0x80:  # no cortar a mitad de un char UTF-8
            cut -= 1
        parts.append(b[:cut].decode("utf-8"))
        b = b[cut:]
    parts.append(b.decode("utf-8"))
    return "\r\n ".join(parts)


def _dt(dt: datetime) -> str:
    """Fecha/hora en UTC como exige iCal para DTSTART/DTEND con Z (ej. 20260722T143000Z)."""
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_calendar(cal_name: str, events: list[dict], now: datetime | None = None) -> str:
    """VCALENDAR con un VEVENT por cita. `events`: dicts con uid/start/summary/description."""
    now = now or datetime.now(UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(cal_name)}",
        f"NAME:{_esc(cal_name)}",
    ]
    for e in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['uid']}",
            f"DTSTAMP:{_dt(now)}",
            f"DTSTART:{_dt(e['start'])}",
            f"DTEND:{_dt(e['start'] + timedelta(minutes=EVENT_MINUTES))}",
            f"SUMMARY:{_esc(e['summary'])}",
            f"DESCRIPTION:{_esc(e['description'])}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


async def get_or_create_token(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    """Token del feed del usuario; lo genera on-demand la primera vez."""
    user = await session.get(Profile, user_id)
    if user is None:
        raise NotFoundError("Usuario no encontrado.")
    if user.calendar_token is None:
        user.calendar_token = uuid.uuid4()
        await session.commit()
    return user.calendar_token


async def rotate_token(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    """Regenera el token (revoca la URL anterior)."""
    user = await session.get(Profile, user_id)
    if user is None:
        raise NotFoundError("Usuario no encontrado.")
    user.calendar_token = uuid.uuid4()
    await session.commit()
    return user.calendar_token


async def user_by_token(session: AsyncSession, token: uuid.UUID) -> Profile | None:
    return await session.scalar(select(Profile).where(Profile.calendar_token == token))


async def agenda_ics_for_user(session: AsyncSession, user: Profile) -> str:
    """.ics de la agenda de `user`: médico → sus citas asignadas; paciente → las suyas."""
    if user.role in ("doctor", "specialist"):
        consults = await consultations_service.list_agenda(session, doctor_user_id=user.id)
        events = [
            {
                "uid": f"{c.id}@{_UID_DOMAIN}",
                "start": c.scheduled_at,
                "summary": f"Cita: {c.patient_name or 'Paciente'}",
                "description": (
                    f"Paciente: {c.patient_name or 'N/D'}\n"
                    f"Motivo: {c.chief_complaint or 'N/D'}\n"
                    f"Código: {c.code}"
                ),
            }
            for c in consults
            if c.scheduled_at
        ]
        return build_calendar("Mi agenda — Médicos por Venezuela", events)

    consults = await consultations_service.list_agenda(session, patient_user_id=user.id)
    events = [
        {
            "uid": f"{c.id}@{_UID_DOMAIN}",
            "start": c.scheduled_at,
            "summary": "Cita médica"
            + (f" con {c.assigned_doctor_name}" if c.assigned_doctor_name else ""),
            "description": (
                f"Médico: {c.assigned_doctor_name or 'Por asignar'}\n"
                f"Motivo: {c.chief_complaint or 'N/D'}\n"
                f"Código: {c.code}"
            ),
        }
        for c in consults
        if c.scheduled_at
    ]
    return build_calendar("Mis citas — Médicos por Venezuela", events)
