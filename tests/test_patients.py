"""Pruebas de integración asíncronas del recurso patients (con aislamiento)."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.patient import Patient
from src.models.profile import Profile
from tests._helpers import auth_headers, make_profile

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
        json={"patient_id": menor_id, "chief_complaint": "Fiebre"},
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
