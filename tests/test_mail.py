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


# --- Difusión (fan-out de interconsultas): stream bulk + BCC por lotes ---


class _SpyClient:
    """Doble que registra cada envío y con qué cliente se pidió (bulk o no)."""

    def __init__(self, registro: list) -> None:
        self._registro = registro

    def send(self, mail: object) -> dict:
        self._registro.append(mail)
        return {"success": True}


def _espiar(monkeypatch: pytest.MonkeyPatch) -> tuple[list, list]:
    """Dobla las DOS factorías por separado, para poder afirmar por cuál stream salió cada
    correo: `enviados` son los mensajes, `streams` dice 'bulk' o 'transaccional' por envío."""
    enviados: list = []
    streams: list = []

    def transaccional() -> _SpyClient:
        streams.append("transaccional")
        return _SpyClient(enviados)

    def bulk() -> _SpyClient:
        streams.append("bulk")
        return _SpyClient(enviados)

    monkeypatch.setattr(settings, "MAILTRAP_API_TOKEN", "token-de-prueba")
    monkeypatch.setattr(mail_service, "_client", transaccional)
    monkeypatch.setattr(mail_service, "_bulk_client", bulk)
    return enviados, streams


async def test_send_bulk_agrupa_en_lotes_por_bcc(monkeypatch: pytest.MonkeyPatch) -> None:
    """120 destinatarios con lotes de 50 = 3 peticiones, no 120. Es el punto del feature:
    una especialidad con cientos de médicos no puede costar cientos de round-trips."""
    enviados, streams = _espiar(monkeypatch)
    monkeypatch.setattr(settings, "MAIL_BULK_BATCH_SIZE", 50)

    destinatarios = [f"medico{i}@example.com" for i in range(120)]
    total = await mail_service.send_bulk(destinatarios, "Interconsulta", "Hay un caso.")

    assert total == 120
    assert len(enviados) == 3  # 50 + 50 + 20
    assert [len(m.bcc) for m in enviados] == [50, 50, 20]
    # Los tres por el stream bulk, ninguno por el transaccional.
    assert streams == ["bulk", "bulk", "bulk"]


async def test_send_bulk_usa_bcc_y_no_expone_a_los_colegas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Los correos de los médicos son datos de colegas: van en BCC, nunca en `to`."""
    enviados, _ = _espiar(monkeypatch)
    await mail_service.send_bulk(["a@example.com", "b@example.com"], "x", "y")

    mail = enviados[0]
    assert [a.email for a in mail.to] == [settings.MAIL_FROM_EMAIL]
    assert {a.email for a in mail.bcc} == {"a@example.com", "b@example.com"}


async def test_send_bulk_deduplica_y_descarta_vacios(monkeypatch: pytest.MonkeyPatch) -> None:
    enviados, _ = _espiar(monkeypatch)
    total = await mail_service.send_bulk(
        ["a@example.com", "a@example.com", "", "b@example.com"], "x", "y"
    )

    assert total == 2
    assert {a.email for a in enviados[0].bcc} == {"a@example.com", "b@example.com"}


async def test_send_bulk_recorta_en_el_tope_y_lo_loguea(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Un recorte silencioso se leería como 'se notificó a todos'."""
    _espiar(monkeypatch)
    monkeypatch.setattr(settings, "MAIL_FANOUT_MAX", 10)
    monkeypatch.setattr(settings, "MAIL_BULK_BATCH_SIZE", 50)

    with caplog.at_level("WARNING", logger="mpv.api"):
        total = await mail_service.send_bulk(
            [f"m{i}@example.com" for i in range(25)], "x", "y", category="interconsulta"
        )

    assert total == 10
    assert any("fanout_truncated" in r.getMessage() for r in caplog.records)


async def test_send_bulk_un_lote_caido_no_aborta_los_demas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort por lote: que Mailtrap rechace uno no puede dejar sin avisar al resto."""
    intentos: list = []

    class MitadRota:
        def send(self, mail: object) -> dict:
            intentos.append(mail)
            if len(intentos) == 1:
                raise RuntimeError("mailtrap caído")
            return {"success": True}

    monkeypatch.setattr(settings, "MAILTRAP_API_TOKEN", "token-de-prueba")
    monkeypatch.setattr(mail_service, "_bulk_client", MitadRota)
    monkeypatch.setattr(settings, "MAIL_BULK_BATCH_SIZE", 2)

    total = await mail_service.send_bulk([f"m{i}@example.com" for i in range(4)], "x", "y")

    assert len(intentos) == 2  # se intentaron los dos lotes
    assert total == 2  # solo cuenta el que salió


async def test_send_bulk_sin_token_no_envia_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAILTRAP_API_TOKEN", "")
    assert await mail_service.send_bulk(["a@example.com"], "x", "y") == 0


async def test_sandbox_gana_sobre_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    """El flag `bulk` no puede desviar los correos de local/tests: con MAILTRAP_INBOX_ID
    definido, el cliente va al inbox de pruebas igual."""
    monkeypatch.setattr(settings, "MAILTRAP_API_TOKEN", "token-de-prueba")
    monkeypatch.setattr(settings, "MAILTRAP_INBOX_ID", "12345")

    cliente = mail_service._bulk_client()
    assert cliente.sandbox is True
    assert cliente.bulk is False


def test_esc_deja_inerte_lo_que_tecleo_una_persona() -> None:
    """SEGURIDAD. `esc` es lo único que separa un dato de formulario de un enlace vivo dentro
    de un correo que la plataforma sí envía. Se prueba aquí, en el módulo donde vive, además de
    en cada cuerpo que lo usa (`test_notifications.py`)."""
    assert mail_service.esc('<a href="http://malo/">Aprobar</a>') == (
        "&lt;a href=&quot;http://malo/&quot;&gt;Aprobar&lt;/a&gt;"
    )
    # `quote=True` no es opcional: sin él, un dato interpolado dentro de un atributo
    # (`<a href="...">`, `title="..."`) puede cerrarlo y añadir atributos propios.
    assert '"' not in mail_service.esc('x" onmouseover="alert(1)')
    # Acepta cualquier cosa, no solo str: los campos opcionales llegan como None o como número
    # y lo que no puede pasar es que revienten la composición del correo.
    assert mail_service.esc(None) == "None"
    assert mail_service.esc(7) == "7"
