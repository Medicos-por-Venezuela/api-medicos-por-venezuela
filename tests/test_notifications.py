"""Composición de los cuerpos de correo de `services/notifications.py`.

Son funciones puras (reciben valores planos, devuelven `(asunto, texto, html)`), así que se
prueban sin sesión y sin IO. Lo que se fija aquí es la frontera de SEGURIDAD: nada de lo que
teclea una persona puede salir como marcado vivo en el HTML.
"""

from datetime import UTC, datetime

from src.services import notifications

# Un enlace completo, no un `<script>`: los clientes de correo no ejecutan JS, pero sí pintan
# un `<a>`. El ataque realista es un enlace con pinta de botón de la plataforma.
VENENO = '<a href="http://malicioso.example">Aprobar ahora</a>'
CUANDO = datetime(2026, 9, 10, 15, 30, tzinfo=UTC)


def _sin_enlace_vivo(html: str) -> None:
    """El dato se conserva, pero inerte: escapado y sin abrir un `<a>` propio."""
    assert "&lt;a href=" in html  # aserción POSITIVA: está escapado, no solo ausente
    assert "malicioso.example" in html  # ...y el dato no se perdió por el camino
    assert '<a href="http://malicioso.example">' not in html


def test_correo_de_cita_escapa_al_paciente_y_al_medico() -> None:
    """SEGURIDAD. `patient_name` sale del formulario PÚBLICO y sin autenticar de la cola
    (`POST /patients`) y `doctor_name` del perfil que el propio médico edita. Sin escapar,
    cualquiera se registra con `<a href="http://malo/">...</a>` de nombre y le mete un enlace
    vivo, con apariencia de venir de la plataforma, al paciente que recibe la cita: phishing
    servido por nosotros.
    """
    _, text, html = notifications._build_email(
        patient_name=VENENO, code="CONS-2026-1", when=CUANDO, doctor_name=None, is_reminder=False
    )
    _sin_enlace_vivo(html)
    assert VENENO in text  # el texto plano no se toca: ahí un `<a>` no es marcado

    _, _, html_medico = notifications._build_email(
        patient_name="María Pérez",
        code="CONS-2026-1",
        when=CUANDO,
        doctor_name=VENENO,
        is_reminder=True,
    )
    _sin_enlace_vivo(html_medico)


def test_difusion_de_interconsulta_escapa_el_motivo() -> None:
    """SEGURIDAD. El motivo lo escribe un médico y esta difusión sale a TODOS los especialistas
    de una especialidad: un solo caso mal intencionado alcanza cientos de bandejas."""
    _, text, html = notifications.interconsultation_broadcast_email(
        specialty_name="Cardiología", chief_complaint=VENENO, age_range="30-39"
    )
    _sin_enlace_vivo(html)
    assert VENENO in text
    # El enlace legítimo al panel sigue siendo un enlace: escapar no puede romper el correo.
    assert f'<a href="{notifications.panel_url()}">' in html


def test_aviso_de_caso_tomado_escapa_al_especialista() -> None:
    """SEGURIDAD. El nombre del especialista viene de su propio perfil y el motivo del caso lo
    escribió el médico tratante; los dos acaban en el HTML del aviso."""
    _, _, html = notifications.interconsultation_taken_email(
        specialist_name=VENENO, specialty_name="Cardiología", chief_complaint="dolor torácico"
    )
    _sin_enlace_vivo(html)

    _, _, html_motivo = notifications.interconsultation_taken_email(
        specialist_name="Dra. Rivas", specialty_name="Cardiología", chief_complaint=VENENO
    )
    _sin_enlace_vivo(html_motivo)


def test_sin_nombre_de_especialista_cae_a_la_especialidad() -> None:
    """El fallback también pasa por el HTML: si el perfil no tiene nombre, el correo dice la
    especialidad en su lugar y no un `None`."""
    subject, text, html = notifications.interconsultation_taken_email(
        specialist_name=None, specialty_name="Cardiología", chief_complaint="dolor torácico"
    )
    assert "Un especialista en Cardiología" in text
    assert "<strong>Un especialista en Cardiología</strong>" in html
    assert "None" not in html
    assert subject


# --- "Tu médico ya está en la sala" (el correo que dispara el claim por video) ---

# Con los dos parámetros del fragmento, es decir CON `&`: es lo que devuelve
# `jitsi.browser_room_url`, y un `&` sin escapar dentro de un `href` es exactamente la clase de
# detalle que rompe un enlace en la mitad de los clientes sin que ningún test lo note.
SALA = (
    "https://meet.medicosporvenezuela.org/vamed-abc"
    "#config.disableDeepLinking=true&config.deeplinking.disabled=true"
)


def test_el_aviso_de_videoconsulta_lleva_el_enlace_como_boton_y_en_claro() -> None:
    """El correo existe para que el paciente ENTRE a la sala: el enlace es su única razón de
    ser. Va dos veces a propósito — como botón y en claro— porque hay clientes que no pintan
    el botón, y quedarse sin forma de llegar sería el mismo problema que esto viene a resolver.
    """
    subject, text, html = notifications.video_ready_email(
        "María Pérez", "Dr. Rivas", SALA, "CONS-2026-1"
    )
    assert "esperando" in subject.lower()
    assert SALA in text
    assert html.count(f'href="{notifications.esc(SALA)}"') == 2
    assert "Entrar a la videoconsulta" in html
    assert "CONS-2026-1" in text and "CONS-2026-1" in html


def test_el_aviso_de_videoconsulta_escapa_los_nombres() -> None:
    """SEGURIDAD. `patient_name` sale del formulario PÚBLICO de la cola y `doctor_name` del
    perfil que el propio médico edita: los mismos dos vectores del correo de cita."""
    _, text, html = notifications.video_ready_email(VENENO, "Dr. Rivas", SALA, "CONS-2026-1")
    _sin_enlace_vivo(html)
    assert VENENO in text

    _, _, html_medico = notifications.video_ready_email("María", VENENO, SALA, "CONS-2026-1")
    _sin_enlace_vivo(html_medico)


def test_el_aviso_de_videoconsulta_sin_nombres_no_dice_none() -> None:
    """El nombre del paciente y el del médico son opcionales en la base. Un correo que
    saludara "Hola None" es peor que uno impersonal."""
    _, text, html = notifications.video_ready_email(None, None, SALA, None)
    assert "None" not in html
    assert "None" not in text
    assert "Tu médico" in text


def test_el_enlace_de_la_sala_va_escapado_dentro_del_href() -> None:
    """El `&` que separa los dos parámetros del fragmento tiene que salir como `&amp;`. Sin
    eso el enlace queda mal formado y hay clientes que lo cortan justo ahí — con el resultado
    de que el paciente aterriza en la sala sin la config que se salta el interstitial de la
    app, que es el paso donde se pierde a la gente en móvil."""
    _, _, html = notifications.video_ready_email("María", "Dr. Rivas", SALA, "CONS-2026-1")
    assert "&amp;config.deeplinking.disabled=true" in html
    assert "true&config" not in html  # el crudo no puede haberse colado
