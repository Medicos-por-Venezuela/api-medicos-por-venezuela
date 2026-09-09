"""Maquetación con la marca de los correos (`services/mail_layout.py`).

Funciones puras: reciben un fragmento y devuelven el documento. Sin sesión y sin IO.

Lo que se fija aquí son las dos cosas que se rompen en silencio y solo se ven en la bandeja de
alguien: que el logotipo salga como imagen (no como SVG, que media docena de clientes descartan)
y que el fabricador de HTML desde texto plano no convierta en marcado vivo lo que teclee una
persona.
"""

import pytest

from src.core.config import settings
from src.services import mail_layout

# Mismo veneno que en `test_notifications`: un enlace completo, no un `<script>`. Los clientes
# de correo no ejecutan JS pero sí pintan un `<a>`, y ese es el ataque realista.
VENENO = '<a href="http://malicioso.example">Aprobar ahora</a>'


def test_el_documento_envuelve_el_fragmento_con_el_banner() -> None:
    salida = mail_layout.render("<p>Hola.</p>")
    assert "<p>Hola.</p>" in salida  # el cuerpo no se pierde por el camino
    assert salida.startswith("<!DOCTYPE html>")
    assert mail_layout.NAVY in salida  # el banner oscuro sobre el que se lee el logotipo blanco
    assert f'<img src="{mail_layout.logo_url()}"' in salida
    assert 'alt="Médicos por Venezuela"' in salida


def test_el_logotipo_es_un_png_y_no_el_svg_del_sitio() -> None:
    """Gmail, Outlook y Yahoo descartan un `<img>` que apunte a un SVG: la cabecera saldría con
    el icono de imagen rota. Es un fallo que no se ve en ningún test de HTML — solo en la
    bandeja de quien lo recibe— así que se fija aquí."""
    assert mail_layout.logo_url().lower().endswith(".png")
    assert ".svg" not in mail_layout.render("<p>x</p>").lower()


def test_el_pie_no_invita_a_responder_al_remitente() -> None:
    """El `From` es un `no-reply@`: nombrarlo en el pie manda las respuestas a un buzón que no
    lee nadie. Mismo criterio que el correo de verificación del médico, que dice explícitamente
    a qué dirección escribir en vez de 'responde a este correo'."""
    salida = mail_layout.render("<p>x</p>")
    assert settings.MAIL_FROM_EMAIL not in salida
    assert "responde a este correo" not in salida.lower()


def test_el_alt_del_logotipo_se_lee_con_las_imagenes_bloqueadas() -> None:
    """Outlook bloquea las imágenes por defecto. El texto alternativo cae sobre el banner
    navy: sin color explícito se pintaría en negro sobre negro y la cabecera quedaría vacía."""
    salida = mail_layout.render("<p>x</p>")
    cabecera = salida[salida.index("<img") : salida.index("</td>", salida.index("<img"))]
    assert "color:#ffffff" in cabecera


def test_html_desde_texto_conserva_los_parrafos_y_enlaza_las_urls() -> None:
    """Los correos que hoy salen solo en texto plano (referencia, interconsulta asignada,
    recordatorio al médico) se maquetan desde su propio texto: sin esto se irían sin marca."""
    salida = mail_layout.html_from_text(
        "Un colega te refirió un paciente.\n\nEntra a tu panel:\nhttps://medicosporvenezuela.org/panel-medico\n"
    )
    assert salida.count("<p>") == 2  # el salto doble separa párrafos...
    assert "<br>" in salida  # ...y el simple es un salto de línea dentro del párrafo
    assert '<a href="https://medicosporvenezuela.org/panel-medico"' in salida


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        # La puntuación que cierra la frase no es parte de la URL: sin recortarla, el enlace
        # apunta a una dirección con un punto de más y no resuelve.
        ("Mira https://ejemplo.org/panel.", "https://ejemplo.org/panel"),
        ("Mira https://ejemplo.org/panel, y luego entra", "https://ejemplo.org/panel"),
    ],
)
def test_html_desde_texto_no_se_traga_la_puntuacion_final(texto: str, esperado: str) -> None:
    assert f'<a href="{esperado}"' in mail_layout.html_from_text(texto)


def test_html_desde_texto_escapa_el_marcado_tecleado() -> None:
    """SEGURIDAD. Un cuerpo de texto plano puede llevar datos que teclea una persona (el motivo
    de una referencia, el nombre de un médico). Si al fabricar el HTML se colara tal cual, este
    módulo convertiría en enlace vivo justo lo que los constructores se cuidan de escapar."""
    salida = mail_layout.html_from_text(f"Motivo: {VENENO}")
    assert "&lt;a href=" in salida  # aserción POSITIVA: escapado, no solo ausente
    assert "malicioso.example" in salida  # ...y el dato se conserva
    assert '<a href="http://malicioso.example">' not in salida


def test_el_boton_lleva_su_enlace_y_el_azul_de_marca() -> None:
    boton = mail_layout.button("https://ejemplo.org/sala", "Entrar")
    assert 'href="https://ejemplo.org/sala"' in boton
    assert mail_layout.BLUE in boton
    assert ">Entrar</a>" in boton
