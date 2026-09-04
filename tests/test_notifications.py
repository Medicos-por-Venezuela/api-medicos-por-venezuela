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
