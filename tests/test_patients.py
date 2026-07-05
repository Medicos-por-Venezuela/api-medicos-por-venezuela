"""Pruebas de integración asíncronas del recurso patients (con aislamiento)."""

from httpx import AsyncClient

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


async def test_list_update_delete_patient(client: AsyncClient) -> None:
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
