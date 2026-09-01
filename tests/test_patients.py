"""Pruebas de integración asíncronas del recurso patients (con aislamiento)."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.patient import Patient
from src.models.profile import Profile
from tests._helpers import add_doctor, any_specialty_id, auth_headers, make_profile

PREFIX = "/api/v1"


async def test_create_and_get_patient(client: AsyncClient) -> None:
    payload = {
        "full_name": "Paciente de Prueba",
        "phone_whatsapp": "+58412000000",
        "affected_zone": "Caracas",
        "needs_tags": ["medicina_general"],
        "consent": True,
    }
    resp = await client.post(f"{PREFIX}/patients", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["full_name"] == "Paciente de Prueba"
    assert body["consent"] is True
    assert body["consent_at"] is not None  # se setea automáticamente
    patient_id = body["id"]

    got = await client.get(f"{PREFIX}/patients/{patient_id}")
    assert got.status_code == 200
    assert got.json()["id"] == patient_id


async def test_create_patient_requires_consent(client: AsyncClient) -> None:
    payload = {
        "full_name": "Sin Consentimiento",
        "phone_whatsapp": "+58412000001",
        "affected_zone": "Maracaibo",
        "consent": False,
    }
    resp = await client.post(f"{PREFIX}/patients", json=payload)
    assert resp.status_code == 400


async def test_create_patient_validation_error(client: AsyncClient) -> None:
    # full_name demasiado corto (min_length=2) -> 422 de Pydantic.
    payload = {
        "full_name": "A",
        "phone_whatsapp": "+58412000002",
        "affected_zone": "Valencia",
    }
    resp = await client.post(f"{PREFIX}/patients", json=payload)
    assert resp.status_code == 422


async def test_get_missing_patient_404(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/patients/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_list_update_delete_patient(
    client: AsyncClient, admin_identity: Profile, db_session: AsyncSession
) -> None:
    created = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Para Editar",
            "phone_whatsapp": "+58412000003",
            "affected_zone": "Mérida",
            "consent": True,
        },
    )
    patient_id = created.json()["id"]

    listed = await client.get(f"{PREFIX}/patients", params={"limit": 100})
    assert listed.status_code == 200
    assert any(p["id"] == patient_id for p in listed.json())

    patched = await client.patch(
        f"{PREFIX}/patients/{patient_id}", json={"full_name": "Editado", "age_range": "30-39"}
    )
    assert patched.status_code == 200
    assert patched.json()["full_name"] == "Editado"
    assert patched.json()["age_range"] == "30-39"

    assert (await client.delete(f"{PREFIX}/patients/{patient_id}")).status_code == 204
    assert (await client.get(f"{PREFIX}/patients/{patient_id}")).status_code == 404
    # Soft delete: la fila sigue en la BD con deleted_at, no se borró (trazabilidad).
    row = (await db_session.execute(select(Patient).where(Patient.id == patient_id))).scalar_one()
    assert row.deleted_at is not None
    # Y ya no aparece en el listado.
    relisted = await client.get(f"{PREFIX}/patients", params={"limit": 100})
    assert all(p["id"] != patient_id for p in relisted.json())

    audit_resp = await client.get(f"{PREFIX}/audit-log", params={"resource": "patients"})
    entries = [e for e in audit_resp.json() if e["resource_id"] == patient_id]
    assert sorted(e["action"] for e in entries) == sorted(["patient.updated", "patient.deleted"])
    assert all(e["actor_user_id"] == str(admin_identity.id) for e in entries)


async def test_list_my_patients_scopes_to_own_and_excludes_archived(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """GET /patients/me (portal del paciente): solo los registros ligados a la cuenta del llamante
    (user_id), sin archivados; nunca los de otro usuario."""
    me = make_profile(role="patient")
    db_session.add(me)
    other_uid = uuid.uuid4()
    mine = Patient(
        full_name="Mío",
        phone_whatsapp="+58412000100",
        affected_zone="Caracas",
        needs_tags=[],
        consent=True,
        user_id=me.id,
    )
    mine_archived = Patient(
        full_name="Mío Archivado",
        phone_whatsapp="+58412000101",
        affected_zone="Caracas",
        needs_tags=[],
        consent=True,
        user_id=me.id,
        deleted_at=datetime.now(UTC),
    )
    other = Patient(
        full_name="De Otro",
        phone_whatsapp="+58412000102",
        affected_zone="Caracas",
        needs_tags=[],
        consent=True,
        user_id=other_uid,
    )
    db_session.add_all([mine, mine_archived, other])
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/patients/me", headers=auth_headers(me.id))
    assert resp.status_code == 200, resp.text
    assert {p["id"] for p in resp.json()} == {str(mine.id)}


async def test_update_missing_patient_404(client: AsyncClient) -> None:
    resp = await client.patch(
        f"{PREFIX}/patients/00000000-0000-0000-0000-000000000000",
        json={"age_range": "40-49"},
    )
    assert resp.status_code == 404


# --- Alergias, carga familiar (menor + adulto responsable) ---


def _adult_payload(**over: object) -> dict:
    base = {
        "full_name": "Adulto Responsable",
        "phone_whatsapp": "+58412000010",
        "affected_zone": "Caracas",
        "cedula": "24319284",
        "consent": True,
    }
    base.update(over)
    return base


async def test_create_patient_con_alergias(client: AsyncClient) -> None:
    resp = await client.post(
        f"{PREFIX}/patients",
        json=_adult_payload(allergies="Penicilina", phone_whatsapp="+58412000011"),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["allergies"] == "Penicilina"


async def test_menor_sin_cedula_hereda_cedula_del_adulto_mas_correlativo(
    client: AsyncClient,
) -> None:
    adulto = await client.post(f"{PREFIX}/patients", json=_adult_payload())
    adulto_id = adulto.json()["id"]

    primero = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Primer Menor",
            "phone_whatsapp": "+58412000010",
            "affected_zone": "Caracas",
            "parent_id": adulto_id,
            "parentesco": "Madre",
            "consent": True,
        },
    )
    assert primero.status_code == 201, primero.text
    assert primero.json()["cedula"] == "243192841"

    segundo = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Segundo Menor",
            "phone_whatsapp": "+58412000010",
            "affected_zone": "Caracas",
            "parent_id": adulto_id,
            "parentesco": "Madre",
            "consent": True,
        },
    )
    assert segundo.status_code == 201, segundo.text
    assert segundo.json()["cedula"] == "243192842"


async def test_menor_con_cedula_propia_no_se_sobreescribe(client: AsyncClient) -> None:
    adulto = await client.post(
        f"{PREFIX}/patients", json=_adult_payload(phone_whatsapp="+58412000012")
    )
    adulto_id = adulto.json()["id"]

    menor = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Menor Con Cedula",
            "phone_whatsapp": "+58412000012",
            "affected_zone": "Caracas",
            "cedula": "V-30000000",
            "parent_id": adulto_id,
            "parentesco": "Padre",
            "consent": True,
        },
    )
    assert menor.status_code == 201, menor.text
    assert menor.json()["cedula"] == "V-30000000"


async def test_parentesco_sin_parent_id_falla_422(client: AsyncClient) -> None:
    resp = await client.post(
        f"{PREFIX}/patients",
        json=_adult_payload(parentesco="Madre", phone_whatsapp="+58412000013"),
    )
    assert resp.status_code == 422


async def test_parent_id_sin_parentesco_falla_422(client: AsyncClient) -> None:
    adulto = await client.post(
        f"{PREFIX}/patients", json=_adult_payload(phone_whatsapp="+58412000014")
    )
    resp = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Menor Sin Parentesco",
            "phone_whatsapp": "+58412000014",
            "affected_zone": "Caracas",
            "parent_id": adulto.json()["id"],
            "consent": True,
        },
    )
    assert resp.status_code == 422


async def test_parent_id_inexistente_falla_400(client: AsyncClient) -> None:
    resp = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Menor Huerfano",
            "phone_whatsapp": "+58412000015",
            "affected_zone": "Caracas",
            "parent_id": "00000000-0000-0000-0000-000000000000",
            "parentesco": "Madre",
            "consent": True,
        },
    )
    assert resp.status_code == 400


async def test_registro_completo_adulto_y_menor_primera_vez(client: AsyncClient) -> None:
    """Flujo real de alta familiar por primera vez: 3 registros en la misma sesión —
    patient del adulto, patient del menor (parent_id -> adulto) y la consulta del menor."""
    adulto = await client.post(
        f"{PREFIX}/patients", json=_adult_payload(phone_whatsapp="+58412000020")
    )
    assert adulto.status_code == 201, adulto.text
    adulto_id = adulto.json()["id"]

    menor = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Menor Primera Vez",
            "phone_whatsapp": "+58412000020",
            "affected_zone": "Caracas",
            "age_range": "7",
            "allergies": "Ninguna conocida",
            "parent_id": adulto_id,
            "parentesco": "Madre",
            "consent": True,
        },
    )
    assert menor.status_code == 201, menor.text
    menor_body = menor.json()
    assert menor_body["parent_id"] == adulto_id
    assert menor_body["parentesco"] == "Madre"
    assert menor_body["cedula"] == "243192841"  # cédula del adulto (24319284) + 1er menor
    menor_id = menor_body["id"]

    consulta = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": menor_id,
            "chief_complaint": "Fiebre",
            "specialty_id": await any_specialty_id(client),
        },
    )
    assert consulta.status_code == 201, consulta.text
    assert consulta.json()["patient_id"] == menor_id

    # La consulta queda asociada al MENOR, no al adulto.
    listado_menor = await client.get(f"{PREFIX}/consultations", params={"patient_id": menor_id})
    assert listado_menor.status_code == 200
    assert any(c["id"] == consulta.json()["id"] for c in listado_menor.json())

    listado_adulto = await client.get(f"{PREFIX}/consultations", params={"patient_id": adulto_id})
    assert listado_adulto.status_code == 200
    assert listado_adulto.json() == []


async def test_list_patients_tolera_email_historico_invalido(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Regresión: un email mal formado en UNA fila no puede tumbar el listado ENTERO.

    `PatientResponse.email` era `EmailStr` y FastAPI valida también la respuesta, así que la
    fila que producción arrastra desde la época de Supabase directo (el navegador escribía
    `patients` con la anon key, sin validar formato) hacía que `GET /patients` devolviera 500
    y el panel admin se quedara sin lista. Se inserta por ORM a propósito: por la API es
    imposible crearla (PatientCreate.email sigue siendo EmailStr).
    """
    legacy = Patient(
        id=uuid.uuid4(),
        full_name="Paciente Legacy",
        phone_whatsapp="+58412000009",
        affected_zone="Caracas",
        email="manuel fegona 29",  # sin '@': inválido para EmailStr
        consent=True,
    )
    db_session.add(legacy)
    await db_session.flush()

    listed = await client.get(f"{PREFIX}/patients", params={"limit": 100})
    assert listed.status_code == 200, listed.text
    fila = next((p for p in listed.json() if p["id"] == str(legacy.id)), None)
    assert fila is not None, "la fila con email inválido debe listarse, no romper la respuesta"
    assert fila["email"] == "manuel fegona 29"  # se devuelve tal cual, sin reescribir el dato

    # La ENTRADA sigue exigiendo formato válido: el 422 es el que debe seguir ocurriendo.
    rechazado = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Email Invalido",
            "phone_whatsapp": "+58412000010",
            "affected_zone": "Caracas",
            "email": "manuel fegona 29",
            "consent": True,
        },
    )
    assert rechazado.status_code == 422, rechazado.text


# --- Pacientes de consultorio (alta por médico, para pedir una interconsulta) ---
#
# Segunda vía de alta, bajo /doctors/me/patients. Lo que se protege acá es la PERTENENCIA:
# el permiso RBAC autoriza la acción, nunca el objeto (regla IDOR del repo).

MIS_PACIENTES = f"{PREFIX}/doctors/me/patients"


def _caso() -> dict:
    """Alta mínima de consultorio: sin teléfono ni zona afectada, que es el punto del feature."""
    return {
        "full_name": "Paciente de Consultorio",
        "age_range": "30-39",
        "allergies": "Penicilina",
        "description": "HTA controlada, sin cirugías previas.",
        "consent": True,
    }


async def test_medico_registra_paciente_sin_telefono_ni_zona(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El formulario corto: el especialista nunca contacta al paciente, así que no se le pide
    PII de contacto. Antes estos dos campos eran NOT NULL."""
    medico = await add_doctor(db_session)

    creado = await client.post(MIS_PACIENTES, json=_caso(), headers=auth_headers(medico.id))
    assert creado.status_code == 201, creado.text
    body = creado.json()
    assert body["phone_whatsapp"] is None
    assert body["affected_zone"] is None
    assert body["created_by_doctor_id"] == str(medico.id)
    assert body["consent_at"] is not None


async def test_alta_de_consultorio_exige_consentimiento(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El médico ATESTIGUA que su paciente autorizó compartir el caso. Sin eso, no hay alta."""
    medico = await add_doctor(db_session)

    sin_consent = await client.post(
        MIS_PACIENTES, json={"full_name": "Sin Consentimiento"}, headers=auth_headers(medico.id)
    )
    assert sin_consent.status_code == 400, sin_consent.text


async def test_alta_publica_sigue_exigiendo_telefono_y_zona(client: AsyncClient) -> None:
    """Relajar el NOT NULL no puede abrirle la puerta a la cola: el alta pública, que sí usa
    esos datos, los sigue exigiendo."""
    incompleto = await client.post(
        f"{PREFIX}/patients", json={"full_name": "Publico Incompleto", "consent": True}
    )
    assert incompleto.status_code == 422, incompleto.text


async def test_listado_solo_devuelve_los_propios(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Dos médicos, un paciente cada uno: ninguno ve el del otro. Tampoco aparecen los
    pacientes de altas públicas, que no tienen dueño médico."""
    medico_a = await add_doctor(db_session)
    medico_b = await add_doctor(db_session)

    a = await client.post(MIS_PACIENTES, json=_caso(), headers=auth_headers(medico_a.id))
    b = await client.post(
        MIS_PACIENTES,
        json={**_caso(), "full_name": "Paciente de B"},
        headers=auth_headers(medico_b.id),
    )
    assert (a.status_code, b.status_code) == (201, 201)

    lista_a = await client.get(MIS_PACIENTES, headers=auth_headers(medico_a.id))
    assert lista_a.status_code == 200
    ids_a = {p["id"] for p in lista_a.json()}
    assert a.json()["id"] in ids_a
    assert b.json()["id"] not in ids_a
    assert all(p["created_by_doctor_id"] == str(medico_a.id) for p in lista_a.json())


async def test_idor_no_puede_leer_editar_ni_archivar_paciente_ajeno(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """IDOR: tener el permiso no da derecho sobre el objeto. Se cubren los TRES caminos de
    acceso al recurso, no solo la lectura — un guard que se olvida de un verbo no es un guard."""
    dueno = await add_doctor(db_session)
    intruso = await add_doctor(db_session)

    creado = await client.post(MIS_PACIENTES, json=_caso(), headers=auth_headers(dueno.id))
    assert creado.status_code == 201
    ajeno = creado.json()["id"]
    headers = auth_headers(intruso.id)

    assert (await client.get(f"{MIS_PACIENTES}/{ajeno}", headers=headers)).status_code == 403
    assert (
        await client.patch(
            f"{MIS_PACIENTES}/{ajeno}", json={"full_name": "Secuestrado"}, headers=headers
        )
    ).status_code == 403
    assert (await client.delete(f"{MIS_PACIENTES}/{ajeno}", headers=headers)).status_code == 403

    # Y el dueño sigue viendo su paciente intacto.
    suyo = await client.get(f"{MIS_PACIENTES}/{ajeno}", headers=auth_headers(dueno.id))
    assert suyo.status_code == 200
    assert suyo.json()["full_name"] == "Paciente de Consultorio"


async def test_paciente_de_alta_publica_no_es_de_ningun_medico(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un paciente que se registró solo no tiene dueño médico: nadie puede reclamarlo por esta
    vía. Si esto devolviera 200, la cola pública quedaría expuesta por la puerta de al lado."""
    medico = await add_doctor(db_session)
    publico = await client.post(
        f"{PREFIX}/patients",
        json={
            "full_name": "Paciente Publico",
            "phone_whatsapp": "+58412777777",
            "affected_zone": "Caracas",
            "consent": True,
        },
    )
    assert publico.status_code == 201

    intento = await client.get(
        f"{MIS_PACIENTES}/{publico.json()['id']}", headers=auth_headers(medico.id)
    )
    assert intento.status_code == 403


async def test_editar_y_archivar_el_propio(client: AsyncClient, db_session: AsyncSession) -> None:
    medico = await add_doctor(db_session)
    creado = await client.post(MIS_PACIENTES, json=_caso(), headers=auth_headers(medico.id))
    pid = creado.json()["id"]
    headers = auth_headers(medico.id)

    editado = await client.patch(
        f"{MIS_PACIENTES}/{pid}", json={"allergies": "Ninguna conocida"}, headers=headers
    )
    assert editado.status_code == 200, editado.text
    assert editado.json()["allergies"] == "Ninguna conocida"

    assert (await client.delete(f"{MIS_PACIENTES}/{pid}", headers=headers)).status_code == 204
    # Baja lógica: el archivado desaparece del listado y de la lectura directa (404, no 403:
    # sigue siendo suyo, ya no existe para la API).
    assert (await client.get(f"{MIS_PACIENTES}/{pid}", headers=headers)).status_code == 404
    assert pid not in {p["id"] for p in (await client.get(MIS_PACIENTES, headers=headers)).json()}


async def test_paciente_no_puede_registrar_pacientes_de_consultorio(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El feature es entre médicos: una cuenta de paciente no tiene el permiso."""
    paciente = make_profile(role="patient")
    db_session.add(paciente)
    await db_session.flush()

    intento = await client.post(MIS_PACIENTES, json=_caso(), headers=auth_headers(paciente.id))
    assert intento.status_code == 403, intento.text


# --- Frontera del listado staff: los de consultorio no son de dominio público ---
#
# `patients.read` lo tiene TODO médico. Sin esta frontera, cualquier colega leería nombre,
# cédula y alergias de los pacientes privados de otro — justo lo que el feature anonimiza en la
# bandeja de interconsultas. Se cubren el LISTADO y el DETALLE: un guard que se olvida de un
# camino no es un guard.


async def _paciente_privado(client: AsyncClient, medico: Profile) -> str:
    creado = await client.post(
        MIS_PACIENTES,
        json={"full_name": "Paciente Privado", "consent": True, "cedula": "V-11223344"},
        headers=auth_headers(medico.id),
    )
    assert creado.status_code == 201, creado.text
    return creado.json()["id"]


async def test_otro_medico_no_ve_pacientes_de_consultorio_en_el_listado_staff(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dueno = await add_doctor(db_session)
    intruso = await add_doctor(db_session)
    privado = await _paciente_privado(client, dueno)

    staff = await client.get(f"{PREFIX}/patients?limit=100", headers=auth_headers(intruso.id))
    assert staff.status_code == 200, staff.text
    assert privado not in {p["id"] for p in staff.json()}
    assert all(p["created_by_doctor_id"] is None for p in staff.json())


async def test_otro_medico_no_puede_leer_el_detalle_de_un_paciente_de_consultorio(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El mismo agujero por el `GET /{id}`: cerrar solo el listado dejaría la puerta abierta."""
    dueno = await add_doctor(db_session)
    intruso = await add_doctor(db_session)
    privado = await _paciente_privado(client, dueno)

    detalle = await client.get(f"{PREFIX}/patients/{privado}", headers=auth_headers(intruso.id))
    assert detalle.status_code == 403, detalle.text


async def test_scope_all_requiere_patients_write(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un médico no puede saltarse el filtro pidiendo `scope=all`."""
    medico = await add_doctor(db_session)
    intento = await client.get(f"{PREFIX}/patients?scope=all", headers=auth_headers(medico.id))
    assert intento.status_code == 403, intento.text


async def test_admin_conserva_la_vision_completa(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El admin (que tiene patients.write) sigue pudiendo operar sobre todo: la frontera protege
    a los médicos entre sí, no ciega a quien administra la plataforma."""
    dueno = await add_doctor(db_session)
    privado = await _paciente_privado(client, dueno)

    # El fixture `client` va autenticado como admin.
    con_todo = await client.get(f"{PREFIX}/patients?scope=all&limit=100")
    assert con_todo.status_code == 200, con_todo.text
    assert privado in {p["id"] for p in con_todo.json()}
    assert (await client.get(f"{PREFIX}/patients/{privado}")).status_code == 200

    # Pero incluso para el admin el default sigue siendo la cola pública.
    por_defecto = await client.get(f"{PREFIX}/patients?limit=100")
    assert privado not in {p["id"] for p in por_defecto.json()}


async def test_el_dueno_sigue_leyendo_su_paciente_por_su_ruta(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cerrar la puerta staff no puede dejar al médico sin acceso a su propio paciente."""
    dueno = await add_doctor(db_session)
    privado = await _paciente_privado(client, dueno)

    suyo = await client.get(f"{MIS_PACIENTES}/{privado}", headers=auth_headers(dueno.id))
    assert suyo.status_code == 200
    assert suyo.json()["cedula"] == "V-11223344"
