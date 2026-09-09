"""URLs de las salas Jitsi (`services/jitsi.py`). Funciones puras, sin sesión ni IO.

`browser_room_url` es el espejo backend de `browserRoomUrl` (lib/jitsi.ts). Existe desde que el
enlace de la sala viaja por correo: hasta entonces siempre lo preparaba el frontend al hacer
clic, y a un enlace de correo no lo toca nadie antes de abrirlo — lo que va escrito es lo que se
abre.
"""

from src.core.config import settings
from src.services import jitsi


def test_una_sala_nueva_apunta_a_nuestra_instancia() -> None:
    url = jitsi.new_room_url()
    assert url.startswith(f"https://{settings.JITSI_DOMAIN}/vamed-")
    assert jitsi.new_room_url() != url  # una sala por consulta, nunca repetida


def test_la_url_de_navegador_salta_el_interstitial_de_la_app() -> None:
    """Sin esta config, un paciente que abre el enlace en el móvil aterriza en una pantalla
    que le pide instalar Jitsi. Ahí se pierde a la gente."""
    salida = jitsi.browser_room_url("https://meet.medicosporvenezuela.org/vamed-abc")
    assert salida.startswith("https://meet.medicosporvenezuela.org/vamed-abc#")
    assert "config.disableDeepLinking=true" in salida
    assert "config.deeplinking.disabled=true" in salida


def test_las_salas_viejas_de_meet_jit_si_se_reescriben() -> None:
    """`meet.jit.si` hoy exige moderador con sesión iniciada: una sala guardada allí deja al
    paciente esperando a un moderador que nunca llega. Se corrige al USAR la URL, así que
    también arregla lo que ya está guardado en la base."""
    salida = jitsi.browser_room_url("https://meet.jit.si/vamed-vieja")
    assert salida.startswith(f"https://{settings.JITSI_DOMAIN}/vamed-vieja")
    assert "meet.jit.si" not in salida


def test_la_url_de_navegador_es_idempotente() -> None:
    """El panel ya la aplica al abrir la sala y el correo la aplica al componerse: pasar dos
    veces por aquí no puede duplicar el fragmento."""
    una = jitsi.browser_room_url("https://meet.medicosporvenezuela.org/vamed-abc")
    assert jitsi.browser_room_url(una) == una


def test_sin_url_no_inventa_una() -> None:
    """Una consulta sin sala devuelve vacío, no un enlace roto: quien llama decide qué hacer
    (el correo, por ejemplo, no se manda)."""
    assert jitsi.browser_room_url("") == ""
