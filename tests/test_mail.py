"""El servicio de correo es best-effort: apagado sin token, y un fallo jamás rompe al caller.
Nunca toca la red: el cliente de Mailtrap se reemplaza por dobles."""

import pytest

from src.core.config import settings
from src.services import mail as mail_service


async def test_send_mail_deshabilitado_sin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAILTRAP_API_TOKEN", "")
    ok = await mail_service.send_mail("a@example.com", "Hola", "texto", category="alerta")
    assert ok is False
    assert mail_service.mail_enabled() is False


async def test_send_mail_envia_con_token(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict = {}

    class FakeClient:
        def send(self, mail: object) -> dict:
            sent["mail"] = mail
            return {"success": True}

    monkeypatch.setattr(settings, "MAILTRAP_API_TOKEN", "token-de-prueba")
    monkeypatch.setattr(mail_service, "_client", lambda: FakeClient())

    ok = await mail_service.send_mail(
        "a@example.com",
        "Recordatorio de consulta",
        "Tu consulta es mañana.",
        category="recordatorio",
    )
    assert ok is True
    assert sent["mail"].subject == "Recordatorio de consulta"
    assert sent["mail"].category == "recordatorio"
    assert sent["mail"].to[0].email == "a@example.com"
    assert sent["mail"].sender.email == settings.MAIL_FROM_EMAIL


async def test_send_mail_fallo_devuelve_false_sin_reventar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoomClient:
        def send(self, mail: object) -> dict:
            raise RuntimeError("mailtrap caído")

    monkeypatch.setattr(settings, "MAILTRAP_API_TOKEN", "token-de-prueba")
    monkeypatch.setattr(mail_service, "_client", lambda: BoomClient())

    ok = await mail_service.send_mail("a@example.com", "x", "y", category="alerta")
    assert ok is False  # el caller decide qué hacer; nunca hay excepción hacia arriba
