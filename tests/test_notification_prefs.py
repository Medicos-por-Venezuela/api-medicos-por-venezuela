"""Tests de las preferencias de notificación: catálogo, opt-out, saneo y respeto en el envío."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.services import notifications
from tests._helpers import make_profile

PREFIX = "/api/v1"


def test_should_send_opt_out() -> None:
    # Ausente = habilitado (opt-out).
    assert notifications.should_send(None, "appointment_reminder", "push") is True
    assert notifications.should_send({}, "appointment_reminder", "email") is True
    # Desactivado explícito.
    prefs = {"appointment_reminder": {"push": False}}
    assert notifications.should_send(prefs, "appointment_reminder", "push") is False
    # Canal que no aplica al evento (confirm no tiene email) → False.
    assert notifications.should_send(None, "appointment_confirm", "email") is False


def test_sanitize_prefs_drops_unknown() -> None:
    dirty = {
        "appointment_reminder": {"push": False, "email": True, "sms": True},  # sms no existe
        "evento_inventado": {"push": True},  # evento fuera del catálogo
        "appointment_confirm": {"email": True},  # email no aplica a confirm
    }
    clean = notifications.sanitize_prefs(dirty)
    assert clean == {"appointment_reminder": {"push": False, "email": True}}


async def test_prefs_endpoint_get_returns_catalog(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    r = await client.get(f"{PREFIX}/me/notification-preferences")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prefs"] == {}  # por defecto vacío = todo activado
    assert set(body["catalog"]) == set(notifications.NOTIFICATION_EVENTS)
    assert body["catalog"]["appointment_reminder"] == ["push", "email"]


async def test_prefs_endpoint_put_saves_and_sanitizes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    payload = {
        "prefs": {
            "appointment_reminder": {"push": True, "email": False},
            "basura": {"push": True},  # se descarta
        }
    }
    r = await client.patch(f"{PREFIX}/me/notification-preferences", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["prefs"] == {"appointment_reminder": {"push": True, "email": False}}

    # Persistió: un GET nuevo lo devuelve.
    again = await client.get(f"{PREFIX}/me/notification-preferences")
    assert again.json()["prefs"] == {"appointment_reminder": {"push": True, "email": False}}


async def test_doctor_event_email_args_respects_pref(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = make_profile(role="doctor")
    doc.email = "esp@example.com"
    db_session.add(doc)
    await db_session.flush()

    # Sin preferencia (opt-out) → devuelve args.
    args = await notifications.doctor_event_email_args(
        db_session, user_id=doc.id, event="referral_received", subject="s", text="t"
    )
    assert args is not None and args["to_email"] == "esp@example.com"

    # Desactivado el correo de ese evento → None (no se envía).
    doc.notification_prefs = {"referral_received": {"email": False}}
    await db_session.flush()
    args2 = await notifications.doctor_event_email_args(
        db_session, user_id=doc.id, event="referral_received", subject="s", text="t"
    )
    assert args2 is None


async def test_doctor_event_email_args_none_without_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = make_profile(role="doctor")  # sin email
    db_session.add(doc)
    await db_session.flush()
    args = await notifications.doctor_event_email_args(
        db_session, user_id=doc.id, event="interconsultation_assigned", subject="s", text="t"
    )
    assert args is None
