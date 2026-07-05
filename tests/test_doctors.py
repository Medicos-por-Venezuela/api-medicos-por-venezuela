"""Pruebas del recurso doctors: registro con verificación SACS/FPV + CRUD.

Las llamadas al SACS/FPV se mockean (sin red). Los professional_types 'Médico' y
'Psicólogo' vienen sembrados por la migración, así que se leen de la BD.
"""

from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.professional_type import ProfessionalType
from src.schemas.psicologo import PsicologoVerificationResponse
from src.schemas.sacs import SacsVerificationResponse
from src.services.doctors import _normalize
from tests._helpers import make_profile

PREFIX = "/api/v1"


async def _type_id(db_session: AsyncSession, kind: str) -> str:
    """id del professional_type sembrado ('medico' | 'psicologo')."""
    rows = (await db_session.execute(select(ProfessionalType))).scalars().all()
    for pt in rows:
        if _normalize(pt.name) == kind:
            return str(pt.id)
    raise AssertionError(f"professional_type '{kind}' no está sembrado")


def _payload(type_id: str, **over: object) -> dict:
    base = {
        "professional_type_id": type_id,
        # Cédula sintética (los tests mockean SACS/FPV, el valor no se consulta);
        # evita colisión con datos reales/mock sembrados en la BD local.
        "cedula": "V-90000001",
        "full_name": "Dr Prueba",
        "phone": "+5804145200715",
        "email": "dr.prueba@test.com",
    }
    base.update(over)
    return base


def _mock_sacs(*, encontrado: bool = True, es_medico: bool = True):
    return patch(
        "src.services.sacs.verificar_sacs",
        AsyncMock(
            return_value=SacsVerificationResponse(encontrado=encontrado, es_medico=es_medico)
        ),
    )


def _mock_fpv(*, encontrado: bool = True):
    return patch(
        "src.services.psicologo.verificar_psicologo",
        AsyncMock(return_value=PsicologoVerificationResponse(encontrado=encontrado)),
    )


# --- Registro + verificación ---


async def test_register_medico_valido_queda_verificado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    type_id = await _type_id(db_session, "medico")
    with _mock_sacs(encontrado=True, es_medico=True):
        resp = await client.post(f"{PREFIX}/doctors", json=_payload(type_id))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["verified"] is True
    assert body["status"] == 1  # activo por defecto


async def test_register_medico_cedula_no_valida_queda_no_verificado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    type_id = await _type_id(db_session, "medico")
    with _mock_sacs(encontrado=False, es_medico=False):
        resp = await client.post(f"{PREFIX}/doctors", json=_payload(type_id))
    assert resp.status_code == 201
    assert resp.json()["verified"] is False


async def test_register_psicologo_valido_queda_verificado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    type_id = await _type_id(db_session, "psicologo")
    with _mock_fpv(encontrado=True):
        resp = await client.post(f"{PREFIX}/doctors", json=_payload(type_id, cedula="V-90000002"))
    assert resp.status_code == 201
    assert resp.json()["verified"] is True


async def test_registro_es_publico(client: AsyncClient, db_session: AsyncSession) -> None:
    """POST no requiere token. Se usa un cliente SIN Authorization, reusando el
    override de get_db (savepoint) que activa la fixture `client` — así no commitea
    datos reales."""
    from httpx import ASGITransport

    from src.main import app

    type_id = await _type_id(db_session, "medico")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        with _mock_sacs():
            resp = await anon.post(
                f"{PREFIX}/doctors", json=_payload(type_id, cedula="V-30111222", email="a@b.com")
            )
    assert resp.status_code == 201


# --- Validación de formato (Pydantic espejo de los CHECK) ---


async def test_honeypot_rechaza_bot(client: AsyncClient, db_session: AsyncSession) -> None:
    """Si el campo trampa `website` llega con valor, se rechaza (400)."""
    type_id = await _type_id(db_session, "medico")
    with _mock_sacs():
        resp = await client.post(
            f"{PREFIX}/doctors", json=_payload(type_id, website="http://spam.example")
        )
    assert resp.status_code == 400


async def test_cedula_formato_invalido_422(client: AsyncClient, db_session: AsyncSession) -> None:
    type_id = await _type_id(db_session, "medico")
    resp = await client.post(f"{PREFIX}/doctors", json=_payload(type_id, cedula="21369660"))
    assert resp.status_code == 422


async def test_telefono_formato_invalido_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    type_id = await _type_id(db_session, "medico")
    resp = await client.post(f"{PREFIX}/doctors", json=_payload(type_id, phone="04145200715"))
    assert resp.status_code == 422


# --- CRUD (staff/admin) ---


async def test_list_get_update_delete(client: AsyncClient, db_session: AsyncSession) -> None:
    type_id = await _type_id(db_session, "medico")
    with _mock_sacs():
        created = await client.post(f"{PREFIX}/doctors", json=_payload(type_id))
    doctor_id = created.json()["id"]

    assert (await client.get(f"{PREFIX}/doctors/{doctor_id}")).status_code == 200

    listed = await client.get(f"{PREFIX}/doctors", params={"status": 1})
    assert listed.status_code == 200
    assert any(d["id"] == doctor_id for d in listed.json())

    # admin expulsa (status = 2)
    patched = await client.patch(f"{PREFIX}/doctors/{doctor_id}", json={"status": 2})
    assert patched.status_code == 200
    assert patched.json()["status"] == 2

    # baja lógica -> luego 404
    assert (await client.delete(f"{PREFIX}/doctors/{doctor_id}")).status_code == 204
    assert (await client.get(f"{PREFIX}/doctors/{doctor_id}")).status_code == 404


async def test_doctor_not_found(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"{PREFIX}/doctors/{missing}")).status_code == 404
    assert (
        await client.patch(f"{PREFIX}/doctors/{missing}", json={"status": 1})
    ).status_code == 404
    assert (await client.delete(f"{PREFIX}/doctors/{missing}")).status_code == 404


# --- Vínculo doctor <-> cuenta (users) por email ---


async def test_doctor_se_liga_a_cuenta_por_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST /doctors resuelve user_id por email si ya existe la cuenta (users)."""
    user = make_profile(role="doctor")
    user.email = "linked.doc@test.com"
    db_session.add(user)
    await db_session.flush()

    type_id = await _type_id(db_session, "medico")
    with _mock_sacs():
        resp = await client.post(
            f"{PREFIX}/doctors",
            json=_payload(type_id, email="linked.doc@test.com", cedula="V-90000003"),
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user_id"] == str(user.id)


async def test_doctor_sin_cuenta_queda_sin_user_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    type_id = await _type_id(db_session, "medico")
    with _mock_sacs():
        resp = await client.post(
            f"{PREFIX}/doctors",
            json=_payload(type_id, email="sincuenta@test.com", cedula="V-90000004"),
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user_id"] is None
