"""Maquetación con la marca de TODOS los correos que sale de la plataforma.

Un solo sitio, y se aplica en `mail.send_mail`, no en cada constructor de cuerpo. Es a
propósito: los cuerpos viven repartidos entre `notifications.py`, `registration_mail.py` y
`interconsultation_requests.py`, y hay tres que hoy se mandan **solo en texto plano**
(`html=None`). Envolver en cada constructor obligaría a acordarse en cada uno, y el correo
número doce saldría sin marca sin que nadie se entere. Envolviendo en el envío, la promesa
"todos los correos llevan la marca" la cumple el código y no la disciplina.

Los constructores siguen devolviendo solo su **fragmento** (`<p>…</p>`), que es lo que sus
tests afirman; el documento completo lo arma `render` alrededor.

**El logotipo es un PNG, no el SVG del sitio.** Gmail (web y móvil), Outlook y Yahoo
descartan un `<img>` que apunte a un SVG: saldría el icono de imagen rota justo en la
cabecera. El PNG lo genera `scripts/build-logo-raster.mjs` en el repo del frontend, desde el
mismo `logo-white.svg`, y se sirve desde el sitio.

**El logotipo es blanco, así que el banner es navy** (`#18202b`, el `--h-navy` de la marca).
Y el PNG viene aplanado sobre ese mismo navy en vez de con transparencia: hay clientes que
pintan su propio fondo detrás de un PNG transparente, y ahí un logotipo blanco desaparece.
Con el fondo dentro de la imagen, el banner se ve igual pase lo que pase.

Reglas de correo que explican por qué esto parece HTML de 2005:
- **Tablas, no flex ni grid.** Outlook de escritorio renderiza con el motor de Word.
- **Estilos en línea.** Gmail recorta el `<head>`; lo único que se le confía es el `@media`
  del final, que es una mejora, no un requisito (sin él el correo se ve bien igual).
- **`bgcolor` además del `background` en CSS**, por el mismo motor de Word.
- **Nada de fuentes web.** Se listan las del sistema; `Nunito Sans` va primera para el cliente
  que la tenga instalada, pero el diseño no depende de ella.
"""

import html as html_mod
import re

from src.core.config import settings

# Paleta. Son los tokens de marca de `styles/globals.css` en el frontend: navy `--h-navy`,
# azul `--h-blue`, gris de fondo `--h-grey-bg`. Si cambian allí, cambian aquí.
NAVY = "#18202b"
BLUE = "#0066fe"
GREY_BG = "#f4f4f4"
TEXT = "#18202b"
MUTED = "#4a5a6e"

_FONT = "'Nunito Sans', -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

# Ancho del banner. 200 px mostrados; el PNG mide el doble para que no se vea borroso en las
# pantallas 2x, que es donde se lee casi todo el correo.
_LOGO_ANCHO = 200

# URLs sueltas dentro de un cuerpo de texto plano, para convertirlas en enlaces. Se aplica
# SOBRE el texto ya escapado, así que `&` ya viene como `&amp;` y no hay que volver a tocarlo.
_URL = re.compile(r"https?://[^\s<]+")


def logo_url() -> str:
    """URL absoluta del logotipo del banner.

    Configurable (`MAIL_LOGO_URL`) pero apuntando SIEMPRE al sitio de producción por defecto, y
    no derivada de `FRONTEND_URL` como `panel_url()`: en local `FRONTEND_URL` es
    `localhost:3000`, y un correo enviado desde una máquina de desarrollo —que con el token de
    Mailtrap real sale de verdad— llegaría con el logotipo roto para quien lo reciba.
    """
    return settings.MAIL_LOGO_URL


def button(url: str, label: str) -> str:
    """Botón de acción, para el HTML de un cuerpo. `url` y `label` deben venir ya escapados.

    Un `<a>` con relleno y no un `<button>` ni un `<input>`: los correos no ejecutan nada, y
    los formularios están bloqueados en casi todos los clientes. El `border-radius` se lo come
    Outlook de escritorio (sale un rectángulo), que es una degradación aceptable: el botón
    sigue siendo un botón azul, grande y pulsable.
    """
    return (
        f'<a href="{url}" style="background:{BLUE};color:#ffffff;font-family:{_FONT};'
        "font-size:17px;font-weight:700;line-height:1;text-decoration:none;border-radius:8px;"
        f'padding:16px 28px;display:inline-block;mso-padding-alt:0">{label}</a>'
    )


def html_from_text(text: str) -> str:
    """Fragmento HTML mínimo a partir de un cuerpo de texto plano.

    Para los correos que hoy solo traen texto (la referencia a un especialista, la
    interconsulta asignada y el recordatorio al médico): sin esto se irían sin marca, que es
    justo lo que este módulo viene a impedir. Escapa primero y enlaza después, así que una URL
    tecleada por una persona no puede convertirse en marcado.
    """
    escapado = html_mod.escape(text, quote=True)
    enlazado = _URL.sub(_enlace, escapado)
    parrafos = [p.strip() for p in enlazado.split("\n\n") if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in parrafos)


def _enlace(m: re.Match[str]) -> str:
    """Convierte una URL suelta en un `<a>`, sin tragarse la puntuación que la cierra."""
    url = m.group(0).rstrip(".,;:)")
    cola = m.group(0)[len(url) :]
    return f'<a href="{url}" style="color:{BLUE}">{url}</a>{cola}'


def render(body_html: str) -> str:
    """Envuelve el fragmento de un correo en el documento completo con la marca.

    Banner navy con el logotipo arriba, la tarjeta blanca con el contenido en medio, y el pie
    —también navy— con la letra pequeña. El pie NO nombra la dirección remitente a propósito:
    es un `no-reply@` y escribirla invita a responder ahí, que es donde el mensaje se pierde
    (el correo de verificación del médico ya lo dice explícitamente).
    """
    logo = html_mod.escape(logo_url(), quote=True)
    sitio = html_mod.escape(settings.FRONTEND_URL.rstrip("/"), quote=True)
    sitio_visible = sitio.replace("https://", "").replace("http://", "")
    return (
        '<!DOCTYPE html><html lang="es"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<meta name="color-scheme" content="light only">'
        "<style>"
        # Única mejora que se le pide al `<head>`: si el cliente la ignora, el correo se ve
        # igual, solo con algo más de aire lateral en pantallas estrechas.
        "@media (max-width:620px){.mpv-pad{padding:24px 18px!important}}"
        "</style></head>"
        f'<body style="margin:0;padding:0;background:{GREY_BG};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{GREY_BG}" style="background:{GREY_BG};margin:0;padding:0;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        # `width:100%` con tope de 600, no `width:600px`: en un móvil el ancho fijo obliga al
        # cliente a alejar el zoom y el correo llega en miniatura. El atributo `width="600"`
        # se queda para Outlook de escritorio, que ignora el CSS de ancho.
        'style="width:100%;max-width:600px;border-collapse:collapse;">'
        # --- Banner: fondo navy, logotipo blanco ---
        f'<tr><td align="center" bgcolor="{NAVY}" '
        f'style="background:{NAVY};padding:28px 24px;border-radius:12px 12px 0 0;">'
        f'<img src="{logo}" width="{_LOGO_ANCHO}" alt="Médicos por Venezuela" '
        f'style="width:{_LOGO_ANCHO}px;max-width:{_LOGO_ANCHO}px;height:auto;display:block;'
        # El `color` del alt: si el cliente bloquea las imágenes (Outlook lo hace por
        # defecto), el texto alternativo se pinta sobre el navy — en negro sería invisible.
        "border:0;outline:none;text-decoration:none;color:#ffffff;font-family:"
        f'{_FONT};font-size:20px;font-weight:800;">'
        "</td></tr>"
        # --- Contenido ---
        f'<tr><td class="mpv-pad" bgcolor="#ffffff" '
        f'style="background:#ffffff;padding:32px 32px 28px;font-family:{_FONT};'
        f'font-size:16px;line-height:1.6;color:{TEXT};">'
        f"{body_html}"
        "</td></tr>"
        # --- Pie: mismo navy, letra pequeña ---
        f'<tr><td align="center" bgcolor="{NAVY}" '
        f'style="background:{NAVY};padding:20px 24px;border-radius:0 0 12px 12px;'
        f'font-family:{_FONT};font-size:12px;line-height:1.6;color:rgba(255,255,255,0.8);">'
        "Médicos por Venezuela · atención médica voluntaria y gratuita<br>"
        f'<a href="{sitio}" style="color:#ffffff;text-decoration:underline;">'
        f"{sitio_visible}</a><br>"
        '<span style="color:rgba(255,255,255,0.62);">'
        "Este mensaje es automático: esta dirección no recibe respuestas."
        "</span>"
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )
