"""Correos de alta: paciente que entra a la cola, y médico que se registra o es aprobado.

Los tests **nunca envían**: `mail.send_mail` se dobla y se captura lo que se le pasó. Sin el
doble tampoco saldría nada (sin `MAILTRAP_API_TOKEN` el servicio es un no-op), pero con él
además se puede afirmar QUÉ se iba a mandar, que es lo que importa aquí.

`MAIL_INTERNAL_RECIPIENTS` se fija por test con `_con_buzones`: viene vacío por defecto a
propósito (ver la decisión 3 del spec) y un test que dependa del `.env` de quien lo corra no
prueba nada.

La pieza que más importa de este fichero es `test_aviso_paciente_no_lleva_pii_clinica`: la
frontera de PII acordada es una decisión de producto, y sin una aserción NEGATIVA que la
sostenga, el primer "añadamos la cédula que es útil" la borra sin que nadie se entere.
"""

import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.consultation import Consultation
from src.models.doctor import Doctor
from src.models.patient import Patient
from src.models.professional_type import ProfessionalType
from src.models.specialty import Specialty
from src.schemas.psicologo import PsicologoVerificationResponse
from src.schemas.sacs import (
    NO_ENCONTRADO,
    SERVICIO_NO_DISPONIBLE,
    SacsVerificationResponse,
)
from src.services import doctors as doctors_service
from src.services import registration_mail
from tests._helpers import make_profile

PREFIX = "/api/v1"

BUZONES = ["ops@ejemplo.org", "otra@ejemplo.org"]


@contextmanager
def _con_buzones(valor: str = ",".join(BUZONES)):
    """Fija los buzones de operación durante el test. `settings` está cacheado por
    `lru_cache`, así que se parchea el atributo y se restaura al salir."""
    original = settings.MAIL_INTERNAL_RECIPIENTS
    settings.MAIL_INTERNAL_RECIPIENTS = valor
    try:
        yield
    finally:
        settings.MAIL_INTERNAL_RECIPIENTS = original


@contextmanager
def _capturar_correos():
    """Dobla el envío y devuelve la lista de lo que se habría mandado.

    Se parchea el nombre YA IMPORTADO en `registration_mail`, no `mail.send_mail`: el módulo
    hizo `from ... import send_mail`, así que una vez importado el nombre es suyo y parchear el
    origen no tendría efecto.
    """
    enviados: list[dict] = []

    async def _fake(to_email, subject, text, html=None, category=None, bcc=None, bulk=False):
        enviados.append(
            {
                "to": to_email,
                "subject": subject,
                "text": text,
                "html": html or "",
                "category": category,
                "bcc": bcc or [],
            }
        )
        return True

    with patch("src.services.registration_mail.send_mail", AsyncMock(side_effect=_fake)):
        yield enviados


# --- Composición: aviso de paciente nuevo (A) ---------------------------------


def test_aviso_paciente_lleva_lo_que_permite_contactarlo() -> None:
    """El correo existe para contactar al paciente SIN entrar al panel: si no trae el
    teléfono, no ahorra el viaje que justifica mandarlo."""
    subject, text, html = registration_mail._build_new_patient(
        patient_name="María Pérez",
        phone="+584140000000",
        zone="Caracas",
        specialty="Pediatría",
        code="CONS-2026-1",
    )
    assert "María Pérez" in subject
    for esperado in ("+584140000000", "Caracas", "Pediatría", "CONS-2026-1"):
        assert esperado in text
        assert esperado in html
    assert "/admin/pacientes" in text  # los destinatarios son operación, no el médico


def test_aviso_paciente_omite_los_campos_vacios() -> None:
    """Un paciente sin zona no debe producir una línea 'Zona: None'."""
    _, text, html = registration_mail._build_new_patient(
        patient_name="Sin Datos", phone=None, zone=None, specialty=None, code="CONS-2026-2"
    )
    assert "None" not in text
    assert "None" not in html
    assert "Zona" not in text


def test_aviso_paciente_no_lleva_pii_clinica() -> None:
    """FRONTERA DE PII (decisión de producto, ver spec).

    Los buzones de operación incluyen un Gmail personal: fuera de la plataforma y fuera del
    `audit_log`. Cédula, alergias y descripción del caso se leen en el panel, que sí deja
    traza. Este test es una aserción NEGATIVA a propósito: es lo único que impide que dentro
    de seis meses alguien añada "un campo útil" y nadie se entere.
    """
    _, text, html = registration_mail._build_new_patient(
        patient_name="María Pérez",
        phone="+584140000000",
        zone="Caracas",
        specialty="Pediatría",
        code="CONS-2026-1",
    )
    cuerpo = text + html
    # La firma de `_build_new_patient` no acepta estos datos, que es la defensa de verdad;
    # esto fija además que no aparezcan por ninguna otra vía (un `str(patient)`, p. ej.).
    for prohibido in ("V-12345678", "Alergia", "penicilina", "dolor de cabeza"):
        assert prohibido not in cuerpo


def test_el_html_escapa_lo_que_escribio_un_desconocido() -> None:
    """SEGURIDAD. `full_name` y compañía salen de formularios PÚBLICOS y sin autenticar, y
    estos correos aterrizan en la bandeja de operación.

    Sin escapar, alguien se registra con `<a href="http://malo/">Aprobar</a>` de nombre y le
    mete a quien lo lea un enlace vivo, con apariencia de venir de la plataforma, dentro de un
    correo que la plataforma sí envió. Es phishing servido por nosotros, y el vector está
    abierto a cualquiera con un navegador.
    """
    veneno = '<a href="http://malicioso.example">Aprobar ahora</a>'
    _, _, html = registration_mail._build_new_patient(
        patient_name=veneno, phone="+58", zone="Caracas", specialty="X", code="C-1"
    )
    assert "<a href" not in html.replace('<a href="https://medicosporvenezuela.org', "")
    assert "&lt;a href=" in html

    _, _, html_medico = registration_mail._build_doctor_registered(
        full_name=veneno,
        cedula="V-1",
        email="x@y.com",
        phone=None,
        professional_type=None,
        specialty=None,
        registered_at=None,
        verified=False,
        reason=None,
    )
    assert "malicioso.example" in html_medico  # el dato se conserva...
    assert '<a href="http://malicioso.example">' not in html_medico  # ...pero inerte


# --- Composición: registro de médico (B/C) ------------------------------------


def test_aviso_medico_no_verificado_lleva_cedula_y_motivo() -> None:
    """Al revés que el del paciente: aquí la cédula ES el asunto del aviso. Sin ella nadie
    puede cotejar el título que llegue por respuesta."""
    subject, text, html = registration_mail._build_doctor_registered(
        full_name="Dr Ejemplo",
        cedula="V-11111111",
        email="dr@ejemplo.com",
        phone="+584140000001",
        professional_type="Médico",
        specialty="Cardiología",
        registered_at=None,
        verified=False,
        reason=doctors_service.NO_ENCONTRADO,
    )
    assert "pendiente" in subject
    for esperado in ("V-11111111", "Dr Ejemplo", "dr@ejemplo.com", "Cardiología"):
        assert esperado in text
    assert registration_mail.DOCTOR_REJECTION_REASONS[doctors_service.NO_ENCONTRADO] in text
    assert "artículo 8" in text  # le dice a operación qué se le pidió al médico
    assert "V-11111111" in html


def test_aviso_medico_verificado_no_lleva_motivo() -> None:
    subject, text, _ = registration_mail._build_doctor_registered(
        full_name="Dr Ok",
        cedula="V-22222222",
        email="ok@ejemplo.com",
        phone=None,
        professional_type="Médico",
        specialty=None,
        registered_at=None,
        verified=True,
        reason=None,
    )
    assert "verificado" in subject
    assert "Motivo" not in text
    assert "Ya puede atender." in text


# --- Composición: correo al médico rechazado (D) ------------------------------


def test_correo_al_medico_pide_los_tres_documentos_y_las_dos_direcciones() -> None:
    with _con_buzones():
        _, text, html = registration_mail._build_doctor_rejected(
            "Dr Ejemplo", "V-11111111", doctors_service.NO_ENCONTRADO
        )
    for documento in registration_mail.REQUIRED_DOCUMENTS:
        assert documento in text
        assert documento in html
    for buzon in BUZONES:
        assert buzon in text
        assert buzon in html
    # Su propia cédula: teclearla mal es la causa más común de que el registro no lo encuentre,
    # y sin verla escrita no tiene cómo darse cuenta.
    assert "V-11111111" in text


def test_correo_al_medico_sin_buzones_cae_al_contacto_publico_y_nunca_a_no_reply() -> None:
    """Sin `MAIL_INTERNAL_RECIPIENTS` el correo al médico igual sale (no depende de esa
    variable), pero la dirección que le da tiene que ser una donde alguien lea.

    Cae a `CONTACT_EMAIL`, **nunca** al remitente: el remitente es `no-reply@`, y pedirle a un
    médico que mande ahí su título es pedirle que lo tire."""
    with _con_buzones(""):
        _, text, html = registration_mail._build_doctor_rejected("Dr X", "V-3", None)
    assert settings.CONTACT_EMAIL in text
    assert settings.MAIL_FROM_EMAIL not in text
    assert settings.MAIL_FROM_EMAIL not in html


def test_el_correo_no_dice_responde_a_este_correo() -> None:
    """El `From` es `no-reply@`: una respuesta no llega a ninguna parte. La instrucción tiene
    que describir lo que de verdad funciona, no lo que suena natural."""
    with _con_buzones():
        _, text, html = registration_mail._build_doctor_rejected("Dr X", "V-3", None)
    assert "responde a este correo" not in text.lower()
    assert "responde a este correo" not in html.lower()
    assert "escríbenos a" in text


@pytest.mark.parametrize("motivo", sorted(registration_mail.DOCTOR_REJECTION_REASONS))
def test_cada_motivo_produce_un_texto_distinto(motivo: str) -> None:
    """El correo D existe para "indicar el porqué". Si dos motivos dieran la misma frase, el
    correo mentiría por omisión. Recorre el diccionario: añadir un motivo sin su frase
    rompe la suite en vez de producir un correo mudo."""
    frase = registration_mail.reason_text(motivo)
    otras = {
        registration_mail.reason_text(m)
        for m in registration_mail.DOCTOR_REJECTION_REASONS
        if m != motivo
    }
    assert frase not in otras
    assert frase != registration_mail._FALLBACK_REASON


def test_motivo_desconocido_no_deja_el_correo_sin_explicacion() -> None:
    assert registration_mail.reason_text("motivo_que_no_existe") == (
        registration_mail._FALLBACK_REASON
    )
    assert registration_mail.reason_text(None) == registration_mail._FALLBACK_REASON


def test_los_cinco_motivos_del_dominio_tienen_frase() -> None:
    """Las constantes de `services.doctors` y las claves del diccionario tienen que coincidir:
    un motivo de dominio sin frase produciría el texto genérico justo cuando sí se sabe qué
    pasó."""
    del_dominio = {
        doctors_service.SIN_TIPO,
        doctors_service.TIPO_NO_VERIFICABLE,
        doctors_service.NO_ENCONTRADO,
        doctors_service.DATOS_INCOMPLETOS,
        doctors_service.SERVICIO_NO_DISPONIBLE,
    }
    assert del_dominio == set(registration_mail.DOCTOR_REJECTION_REASONS)


# --- Composición: correo al médico aprobado (E) -------------------------------


def test_correo_de_aprobacion_enlaza_al_panel_medico() -> None:
    subject, text, html = registration_mail._build_doctor_approved("Dr Ejemplo")
    assert "aprobado" in subject
    assert "/panel-medico" in text
    assert "/panel-medico" in html


# --- `*_mail_args`: las guardas -----------------------------------------------


async def _consulta_publica(db_session: AsyncSession, **patient_over) -> Consultation:
    """Paciente de la cola pública + su consulta, sin pasar por HTTP."""
    campos = {
        "full_name": "Paciente Correo",
        "phone_whatsapp": "+584140000009",
        "affected_zone": "Caracas",
        "consent": True,
        "cedula": "V-12345678",
        "allergies": "penicilina",
        "description": "dolor de cabeza",
    }
    campos.update(patient_over)
    patient = Patient(**campos)
    db_session.add(patient)
    await db_session.flush()
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()
    consultation = Consultation(
        patient_id=patient.id, specialty_id=specialty.id, code=f"T-{uuid.uuid4().hex[:8]}"
    )
    db_session.add(consultation)
    await db_session.flush()
    return consultation


async def test_args_paciente_sin_buzones_no_avisa(db_session: AsyncSession) -> None:
    consultation = await _consulta_publica(db_session)
    with _con_buzones(""):
        assert await registration_mail.new_patient_mail_args(db_session, consultation) is None


async def test_args_paciente_de_consultorio_no_avisa(db_session: AsyncSession) -> None:
    """El paciente de consultorio es privado de su médico: la plataforma no lo comparte, ni
    siquiera con operación. Mismo criterio que `patients.list_patients`."""
    doctor_user = make_profile(role="doctor")
    db_session.add(doctor_user)
    await db_session.flush()
    consultation = await _consulta_publica(db_session, created_by_doctor_id=doctor_user.id)
    with _con_buzones():
        assert await registration_mail.new_patient_mail_args(db_session, consultation) is None


async def test_args_consulta_agendada_no_avisa(db_session: AsyncSession) -> None:
    """Una cita agendada ya tiene su correo por `notifications`; avisar aquí lo duplicaría, y
    además una cita futura no es la urgencia que persigue este aviso."""
    from datetime import UTC, datetime, timedelta

    consultation = await _consulta_publica(db_session)
    consultation.scheduled_at = datetime.now(UTC) + timedelta(days=1)
    await db_session.flush()
    with _con_buzones():
        assert await registration_mail.new_patient_mail_args(db_session, consultation) is None


async def test_args_paciente_publico_resuelve_valores_planos(db_session: AsyncSession) -> None:
    """Los args tienen que ser valores planos: el BackgroundTask corre con la sesión ya
    cerrada y un objeto ORM ahí dentro explotaría al tocar cualquier atributo perezoso."""
    consultation = await _consulta_publica(db_session)
    with _con_buzones():
        args = await registration_mail.new_patient_mail_args(db_session, consultation)
    assert args is not None
    assert args["patient_name"] == "Paciente Correo"
    assert args["phone"] == "+584140000009"
    assert args["specialty"]  # nombre resuelto, no el uuid
    assert all(not hasattr(v, "__table__") for v in args.values())


# --- Integración: POST /consultations -----------------------------------------


async def test_alta_publica_encola_el_aviso(client: AsyncClient, db_session: AsyncSession) -> None:
    patient = Patient(
        full_name="Paciente Alta",
        phone_whatsapp="+584140000010",
        affected_zone="Miranda",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()

    with _con_buzones(), _capturar_correos() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": str(patient.id), "specialty_id": str(specialty.id)},
        )
    assert resp.status_code == 201, resp.text
    assert len(enviados) == 1
    correo = enviados[0]
    assert correo["to"] == BUZONES[0]
    assert correo["bcc"] == BUZONES[1:]  # el resto en copia oculta: no se ven entre sí
    assert "Paciente Alta" in correo["subject"]
    assert "+584140000010" in correo["text"]


async def test_alta_publica_sin_buzones_no_manda_nada(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient = Patient(
        full_name="Paciente Sin Aviso",
        phone_whatsapp="+584140000011",
        affected_zone="Miranda",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()

    with _con_buzones(""), _capturar_correos() as enviados:
        resp = await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": str(patient.id), "specialty_id": str(specialty.id)},
        )
    assert resp.status_code == 201, resp.text
    assert enviados == []


async def test_el_alta_sobrevive_a_un_fallo_de_correo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Best-effort: Mailtrap caído no puede impedir que un paciente entre a la cola."""
    patient = Patient(
        full_name="Paciente Resiliencia",
        phone_whatsapp="+584140000012",
        affected_zone="Zulia",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()

    boom = AsyncMock(side_effect=RuntimeError("mailtrap caído"))
    with _con_buzones(), patch("src.services.registration_mail.send_mail", boom):
        resp = await client.post(
            f"{PREFIX}/consultations",
            json={"patient_id": str(patient.id), "specialty_id": str(specialty.id)},
        )
    assert resp.status_code == 201, resp.text


# --- Integración: POST /doctors -----------------------------------------------


async def _type_id(db_session: AsyncSession, kind: str) -> str:
    rows = (await db_session.execute(select(ProfessionalType))).scalars().all()
    for pt in rows:
        if doctors_service._normalize(pt.name) == kind:
            return str(pt.id)
    raise AssertionError(f"professional_type '{kind}' no está sembrado")


def _doctor_payload(type_id: str, **over) -> dict:
    base = {
        "professional_type_id": type_id,
        "cedula": f"V-9{uuid.uuid4().int % 10**7:07d}",
        "full_name": "Dr Correo",
        "phone": "+5804145200799",
        "email": f"dr.correo.{uuid.uuid4().hex[:8]}@test.com",
    }
    base.update(over)
    return base


def _mock_sacs(**over):
    campos = {
        "encontrado": True,
        "es_medico": True,
        "nombre": "JUAN",
        "apellido": "PEREZ",
        "licencia": "MPPS-77777",
    }
    campos.update(over)
    return patch(
        "src.services.sacs.verificar_sacs",
        AsyncMock(return_value=SacsVerificationResponse(**campos)),
    )


async def test_registro_verificado_manda_aviso_interno_y_bienvenida(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    type_id = await _type_id(db_session, "medico")
    payload = _doctor_payload(type_id)

    with _con_buzones(), _capturar_correos() as enviados, _mock_sacs():
        resp = await client.post(f"{PREFIX}/doctors", json=payload)
    assert resp.status_code == 201, resp.text
    assert resp.json()["verified"] is True
    assert len(enviados) == 2

    interno = next(c for c in enviados if c["to"] == BUZONES[0])
    al_medico = next(c for c in enviados if c["to"] == payload["email"])
    assert "verificado" in interno["subject"]
    assert payload["cedula"] in interno["text"]
    assert "aprobado" in al_medico["subject"]
    assert "/panel-medico" in al_medico["text"]


async def test_aviso_interno_resuelve_el_nombre_de_la_especialidad(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El correo lleva el NOMBRE de la especialidad, no su uuid: quien lo lee es una persona
    decidiendo a quién asignar, y un identificador no le dice nada."""
    type_id = await _type_id(db_session, "medico")
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()
    payload = _doctor_payload(type_id, specialty_id=str(specialty.id))

    with _con_buzones(), _capturar_correos() as enviados, _mock_sacs():
        resp = await client.post(f"{PREFIX}/doctors", json=payload)
    assert resp.status_code == 201, resp.text

    interno = next(c for c in enviados if c["to"] == BUZONES[0])
    assert specialty.name in interno["text"]
    assert str(specialty.id) not in interno["text"]


async def test_registro_no_verificado_manda_aviso_interno_y_peticion_de_papeles(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    type_id = await _type_id(db_session, "medico")
    payload = _doctor_payload(type_id)

    with (
        _con_buzones(),
        _capturar_correos() as enviados,
        _mock_sacs(encontrado=False, es_medico=False, error_kind=NO_ENCONTRADO),
    ):
        resp = await client.post(f"{PREFIX}/doctors", json=payload)
    assert resp.status_code == 201, resp.text
    assert resp.json()["verified"] is False
    assert len(enviados) == 2

    interno = next(c for c in enviados if c["to"] == BUZONES[0])
    al_medico = next(c for c in enviados if c["to"] == payload["email"])
    assert "pendiente" in interno["subject"]
    assert payload["cedula"] in interno["text"]
    assert (
        registration_mail.DOCTOR_REJECTION_REASONS[doctors_service.NO_ENCONTRADO]
        in interno["text"]
    )
    for documento in registration_mail.REQUIRED_DOCUMENTS:
        assert documento in al_medico["text"]
    assert payload["cedula"] in al_medico["text"]


async def test_sacs_caido_le_dice_al_medico_que_no_es_culpa_suya(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El caso que justifica todo el motivo tipado: hoy un SACS caído producía el mismo
    rechazo silencioso que una cédula falsa, y el médico no podía distinguirlos."""
    type_id = await _type_id(db_session, "medico")
    payload = _doctor_payload(type_id)

    with (
        _con_buzones(),
        _capturar_correos() as enviados,
        _mock_sacs(encontrado=False, es_medico=False, error_kind=SERVICIO_NO_DISPONIBLE),
    ):
        resp = await client.post(f"{PREFIX}/doctors", json=payload)
    assert resp.status_code == 201, resp.text

    al_medico = next(c for c in enviados if c["to"] == payload["email"])
    esperado = registration_mail.DOCTOR_REJECTION_REASONS[doctors_service.SERVICIO_NO_DISPONIBLE]
    assert esperado in al_medico["text"]
    # Y lo contrario: no se le acusa de tener una cédula que no existe.
    no_esperado = registration_mail.DOCTOR_REJECTION_REASONS[doctors_service.NO_ENCONTRADO]
    assert no_esperado not in al_medico["text"]


async def test_tipo_no_verificable_tiene_su_propio_motivo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un tipo profesional sin registro en línea (p. ej. nutricionista) no es un rechazo: es
    'lo revisamos a mano'. Sin motivo tipado, este médico recibía el mismo texto que uno con
    la cédula mal escrita."""
    ptype = ProfessionalType(name=f"Nutricionista {uuid.uuid4().hex[:6]}")
    db_session.add(ptype)
    await db_session.flush()

    with _con_buzones(), _capturar_correos() as enviados:
        resp = await client.post(f"{PREFIX}/doctors", json=_doctor_payload(str(ptype.id)))
    assert resp.status_code == 201, resp.text

    al_medico = enviados[-1]
    esperado = registration_mail.DOCTOR_REJECTION_REASONS[doctors_service.TIPO_NO_VERIFICABLE]
    assert esperado in al_medico["text"]


async def test_registro_sin_buzones_igual_le_escribe_al_medico(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El correo al médico NO depende de `MAIL_INTERNAL_RECIPIENTS`: aunque operación no tenga
    buzón, quien se registró merece su respuesta."""
    type_id = await _type_id(db_session, "medico")
    payload = _doctor_payload(type_id)

    with _con_buzones(""), _capturar_correos() as enviados, _mock_sacs():
        resp = await client.post(f"{PREFIX}/doctors", json=payload)
    assert resp.status_code == 201, resp.text
    assert len(enviados) == 1
    assert enviados[0]["to"] == payload["email"]


async def test_el_registro_sobrevive_a_un_fallo_de_correo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    type_id = await _type_id(db_session, "medico")
    boom = AsyncMock(side_effect=RuntimeError("mailtrap caído"))
    with _con_buzones(), patch("src.services.registration_mail.send_mail", boom), _mock_sacs():
        resp = await client.post(f"{PREFIX}/doctors", json=_doctor_payload(type_id))
    assert resp.status_code == 201, resp.text


async def test_psicologo_no_encontrado_propaga_su_motivo(db_session: AsyncSession) -> None:
    """La FPV va por el mismo camino que el SACS; sin este test, `_check_in_fpv` podría
    quedarse con el motivo por defecto y nadie lo notaría."""
    with patch(
        "src.services.psicologo.verificar_psicologo",
        AsyncMock(
            return_value=PsicologoVerificationResponse(
                encontrado=False, error="no está", error_kind=NO_ENCONTRADO
            )
        ),
    ):
        check = await doctors_service._check_in_fpv("V-1")
    assert check.verified is False
    assert check.reason == doctors_service.NO_ENCONTRADO


async def test_datos_incompletos_cuando_el_registro_responde_sin_licencia(
    db_session: AsyncSession,
) -> None:
    """Encontrado pero sin licencia: fail-closed igual, pero el motivo es otro y el correo lo
    dice ('hay que verificarla a mano', no 'tu cédula no existe')."""
    with _mock_sacs(licencia=None):
        check = await doctors_service._check_in_sacs("V-1")
    assert check.verified is False
    assert check.reason == doctors_service.DATOS_INCOMPLETOS


# --- Integración: POST /doctors/{id}/approve ----------------------------------


async def _ficha_no_verificada(client: AsyncClient, db_session: AsyncSession) -> str:
    """Registra un médico que el SACS no valida y devuelve el id de su ficha."""
    type_id = await _type_id(db_session, "medico")
    payload = _doctor_payload(type_id)
    with _con_buzones(""), _capturar_correos(), _mock_sacs(encontrado=False, es_medico=False):
        resp = await client.post(f"{PREFIX}/doctors", json=payload)
    assert resp.status_code == 201, resp.text
    # `approve` exige cédula Y licencia: el registro no verificado no copia la del payload.
    doctor = await db_session.get(Doctor, uuid.UUID(resp.json()["id"]))
    doctor.license = "MPPS-99999"
    await db_session.flush()
    return resp.json()["id"]


async def test_aprobar_avisa_al_medico_una_sola_vez(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Idempotencia observable: el endpoint acepta dos clics (y ambos quedan auditados), pero
    solo el primero cambia el estado, así que solo el primero escribe."""
    doctor_id = await _ficha_no_verificada(client, db_session)

    with _con_buzones(), _capturar_correos() as enviados:
        primera = await client.post(f"{PREFIX}/doctors/{doctor_id}/approve")
        segunda = await client.post(f"{PREFIX}/doctors/{doctor_id}/approve")

    assert primera.status_code == 200, primera.text
    assert segunda.status_code == 200, segunda.text
    assert len(enviados) == 1, "el segundo clic no debe producir un segundo 'ya puedes entrar'"
    assert "aprobado" in enviados[0]["subject"]
    # A operación no se le avisa: quien aprobó estaba mirando la pantalla.
    assert enviados[0]["to"] not in BUZONES


async def test_aprobar_a_un_medico_sin_correo_no_rompe(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Las fichas backfilleadas nacieron sin email (el contacto vivía en la cuenta) y son justo
    las antiguas que un admin puede estar aprobando ahora."""
    doctor = Doctor(
        full_name="Dr Sin Correo",
        cedula=f"V-8{uuid.uuid4().int % 10**7:07d}",
        license="MPPS-88888",
        status=1,
        verified=False,
        email=None,
        user_id=None,
    )
    db_session.add(doctor)
    await db_session.flush()

    with _con_buzones(), _capturar_correos() as enviados:
        resp = await client.post(f"{PREFIX}/doctors/{doctor.id}/approve")
    assert resp.status_code == 200, resp.text
    assert enviados == []


async def test_aprobar_cae_al_correo_de_la_cuenta_si_la_ficha_no_lo_tiene(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`doctors.email` -> `users.email`: es lo que hace que las fichas antiguas sí reciban el
    aviso en vez de quedarse mudas."""
    cuenta = make_profile(role="doctor")
    cuenta.email = f"cuenta.{uuid.uuid4().hex[:8]}@test.com"
    db_session.add(cuenta)
    await db_session.flush()
    doctor = Doctor(
        full_name="Dr Correo En La Cuenta",
        cedula=f"V-7{uuid.uuid4().int % 10**7:07d}",
        license="MPPS-77778",
        status=1,
        verified=False,
        email=None,
        user_id=cuenta.id,
    )
    db_session.add(doctor)
    await db_session.flush()

    with _con_buzones(), _capturar_correos() as enviados:
        resp = await client.post(f"{PREFIX}/doctors/{doctor.id}/approve")
    assert resp.status_code == 200, resp.text
    assert len(enviados) == 1
    assert enviados[0]["to"] == cuenta.email


async def test_aprobar_sobrevive_a_un_fallo_de_correo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doctor_id = await _ficha_no_verificada(client, db_session)
    boom = AsyncMock(side_effect=RuntimeError("mailtrap caído"))
    with _con_buzones(), patch("src.services.registration_mail.send_mail", boom):
        resp = await client.post(f"{PREFIX}/doctors/{doctor_id}/approve")
    assert resp.status_code == 200, resp.text


# --- Config -------------------------------------------------------------------


def test_buzones_internos_se_parten_y_limpian() -> None:
    with _con_buzones(" a@x.com , b@x.com ,, "):
        assert settings.internal_mail_recipients == ["a@x.com", "b@x.com"]


def test_sin_buzones_la_lista_es_vacia() -> None:
    """Vacía por defecto: es lo que impide que un entorno de pruebas con token de Mailtrap le
    escriba de verdad a los buzones reales de operación."""
    with _con_buzones(""):
        assert settings.internal_mail_recipients == []


async def test_envio_interno_sin_buzones_devuelve_false() -> None:
    with _con_buzones(""), _capturar_correos() as enviados:
        enviado = await registration_mail._send_internal("s", "t", "h", category="x")
    assert enviado is False
    assert enviados == []
