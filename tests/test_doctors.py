"""Pruebas del recurso doctors: registro con verificación SACS/FPV + CRUD.

Las llamadas al SACS/FPV se mockean (sin red). Los professional_types 'Médico' y
'Psicólogo' vienen sembrados por la migración, así que se leen de la BD.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.doctor import Doctor
from src.models.professional_type import ProfessionalType
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.schemas.psicologo import PsicologoVerificationResponse
from src.schemas.sacs import SacsVerificationResponse
from src.services.doctors import _normalize
from tests._helpers import auth_headers, make_profile

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


async def test_list_get_update_delete(
    client: AsyncClient, db_session: AsyncSession, admin_identity: Profile
) -> None:
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

    audit_resp = await client.get(f"{PREFIX}/audit-log", params={"resource": "doctors"})
    entries = [e for e in audit_resp.json() if e["resource_id"] == doctor_id]
    assert sorted(e["action"] for e in entries) == sorted(["doctor.updated", "doctor.deleted"])
    assert all(e["actor_user_id"] == str(admin_identity.id) for e in entries)


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


# --- Propagación doctors -> users (specialty/country/license/whatsapp) ---


async def test_create_doctor_propaga_datos_a_la_cuenta(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST /doctors, ligado a una cuenta, propaga specialty/country/license/phone
    a users (fuente de verdad para médicos del flujo nuevo; ver _sync_user_from_doctor).
    """
    user = make_profile(role="doctor")
    user.email = "sync.doc@test.com"
    db_session.add(user)
    await db_session.flush()

    type_id = await _type_id(db_session, "medico")
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()

    with _mock_sacs():
        resp = await client.post(
            f"{PREFIX}/doctors",
            json=_payload(
                type_id,
                email="sync.doc@test.com",
                cedula="V-90000005",
                specialty_id=str(specialty.id),
                license="MPPS-12345",
                phone="+584140009999",
                country_of_residence="Venezuela",
            ),
        )
    assert resp.status_code == 201, resp.text

    await db_session.refresh(user)
    assert user.specialty == specialty.name
    assert user.medical_license == "MPPS-12345"
    assert user.whatsapp_number == "+584140009999"
    assert user.country == "Venezuela"


async def test_update_doctor_repropaga_datos_a_la_cuenta(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """PATCH /doctors/{id} también re-sincroniza users si cambia specialty/etc."""
    user = make_profile(role="doctor")
    user.email = "resync.doc@test.com"
    db_session.add(user)
    await db_session.flush()

    type_id = await _type_id(db_session, "medico")
    specialties = (await db_session.execute(select(Specialty).limit(2))).scalars().all()
    assert len(specialties) == 2, "el catálogo necesita al menos 2 specialties para este test"

    with _mock_sacs():
        created = await client.post(
            f"{PREFIX}/doctors",
            json=_payload(
                type_id,
                email="resync.doc@test.com",
                cedula="V-90000006",
                specialty_id=str(specialties[0].id),
            ),
        )
    doctor_id = created.json()["id"]

    patched = await client.patch(
        f"{PREFIX}/doctors/{doctor_id}", json={"specialty_id": str(specialties[1].id)}
    )
    assert patched.status_code == 200, patched.text

    await db_session.refresh(user)
    assert user.specialty == specialties[1].name


async def test_doctor_sin_cuenta_no_falla_al_sincronizar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Sin user_id (nadie con ese email), _sync_user_from_doctor no debe romper el alta."""
    type_id = await _type_id(db_session, "medico")
    with _mock_sacs():
        resp = await client.post(
            f"{PREFIX}/doctors",
            json=_payload(type_id, email="huerfano.doc@test.com", cedula="V-90000007"),
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user_id"] is None


# --- Perfil propio del médico (GET/PATCH /doctors/me) ---
# IDOR: el recurso se resuelve del `user_id` del JWT; se autentica por-request con
# auth_headers(user.id) para actuar como ese médico (el `client` es admin por defecto).


async def _seed_doctor_with_account(
    db_session: AsyncSession, *, cedula: str, specialty_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, Doctor]:
    """Crea una cuenta (users) de médico + su fila en doctors ligada por user_id."""
    user = make_profile(role="doctor")
    db_session.add(user)
    await db_session.flush()
    type_id = uuid.UUID(await _type_id(db_session, "medico"))
    doctor = Doctor(
        user_id=user.id,
        professional_type_id=type_id,
        specialty_id=specialty_id,
        cedula=cedula,
        full_name="Dr Propio",
        license="MPPS-0001",
        verified=True,
    )
    db_session.add(doctor)
    await db_session.flush()
    return user.id, doctor


async def test_get_me_desde_doctors(client: AsyncClient, db_session: AsyncSession) -> None:
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()
    user_id, _ = await _seed_doctor_with_account(
        db_session, cedula="V-90001001", specialty_id=specialty.id
    )
    resp = await client.get(f"{PREFIX}/doctors/me", headers=auth_headers(user_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "doctor"
    assert body["cedula"] == "V-90001001"
    assert body["specialty_id"] == str(specialty.id)
    assert body["specialty"] == specialty.name


async def test_get_me_fallback_a_users(client: AsyncClient, db_session: AsyncSession) -> None:
    """Médico sin fila en doctors -> el perfil sale de su cuenta en users."""
    user = make_profile(role="doctor", specialty="Cardiología")
    user.medical_license = "MPPS-USR-9"
    db_session.add(user)
    await db_session.flush()
    resp = await client.get(f"{PREFIX}/doctors/me", headers=auth_headers(user.id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "user"
    assert body["cedula"] is None
    assert body["specialty_id"] is None
    assert body["specialty"] == "Cardiología"
    assert body["license"] == "MPPS-USR-9"


async def test_get_me_paciente_404(client: AsyncClient, db_session: AsyncSession) -> None:
    """Un no-médico sin fila en doctors no tiene 'perfil de médico'."""
    patient = make_profile(role="patient")
    db_session.add(patient)
    await db_session.flush()
    resp = await client.get(f"{PREFIX}/doctors/me", headers=auth_headers(patient.id))
    assert resp.status_code == 404


async def test_me_sin_token_401(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/doctors/me", headers={"Authorization": ""})
    assert resp.status_code == 401


async def test_patch_me_actualiza_y_propaga_a_users(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    specialties = (await db_session.execute(select(Specialty).limit(2))).scalars().all()
    assert len(specialties) == 2
    user_id, _ = await _seed_doctor_with_account(
        db_session, cedula="V-90001002", specialty_id=specialties[0].id
    )
    resp = await client.patch(
        f"{PREFIX}/doctors/me",
        headers=auth_headers(user_id),
        json={
            "full_name": "Dra Actualizada",
            "license": "MPPS-9999",
            "specialty_id": str(specialties[1].id),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["full_name"] == "Dra Actualizada"
    assert body["specialty"] == specialties[1].name
    assert body["verified"] is True  # no cambió la cédula -> no re-verifica

    # la especialidad (nombre) se propagó a users vía _sync_user_from_doctor
    user = await db_session.get(Profile, user_id)
    assert user.specialty == specialties[1].name
    assert user.medical_license == "MPPS-9999"


async def test_patch_me_cambiar_cedula_reverifica(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cambiar la cédula re-consulta SACS/FPV y recalcula verified (fail-closed)."""
    user_id, _ = await _seed_doctor_with_account(db_session, cedula="V-90001003")
    with _mock_sacs(encontrado=False, es_medico=False):
        resp = await client.patch(
            f"{PREFIX}/doctors/me",
            headers=auth_headers(user_id),
            json={"cedula": "V-90001099"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cedula"] == "V-90001099"
    assert body["verified"] is False


async def test_patch_me_fallback_users_actualiza(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    specialty = (await db_session.execute(select(Specialty).limit(1))).scalar_one()
    user = make_profile(role="doctor")
    db_session.add(user)
    await db_session.flush()
    resp = await client.patch(
        f"{PREFIX}/doctors/me",
        headers=auth_headers(user.id),
        json={
            "full_name": "Dr Users",
            "license": "MPPS-USR-77",
            "specialty_id": str(specialty.id),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["specialty"] == specialty.name
    assert body["license"] == "MPPS-USR-77"
    await db_session.refresh(user)
    assert user.specialty == specialty.name
    assert user.full_name == "Dr Users"
    # 'license' del schema se mapea a users.medical_license
    assert user.medical_license == "MPPS-USR-77"


async def test_patch_me_fallback_users_cedula_400(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """En la fuente users no hay tipo/cédula que verificar: editarla es 400."""
    user = make_profile(role="doctor")
    db_session.add(user)
    await db_session.flush()
    resp = await client.patch(
        f"{PREFIX}/doctors/me",
        headers=auth_headers(user.id),
        json={"cedula": "V-90001004"},
    )
    assert resp.status_code == 400


async def test_patch_me_rechaza_campos_no_permitidos_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """status/verified/email/phone no son auto-editables (extra='forbid')."""
    user_id, _ = await _seed_doctor_with_account(db_session, cedula="V-90001005")
    for forbidden in ({"status": 0}, {"verified": True}, {"email": "x@y.com"}):
        resp = await client.patch(
            f"{PREFIX}/doctors/me", headers=auth_headers(user_id), json=forbidden
        )
        assert resp.status_code == 422, f"{forbidden} -> {resp.status_code}"


# --- Pool de médicos (GET /doctors/pool) ---
#
# La BD local tiene datos de prod (~2849 doctores), así que se asserta por PERTENENCIA de id,
# no por totales absolutos, y se aíslan las filas sembradas con el tipo 'nutricionista' (casi
# ningún doctor real lo usa) para que el filtro por professional_type_id devuelva solo lo nuestro.


async def _pool_doctor(
    db: AsyncSession,
    *,
    online: bool,
    type_id: str,
    specialty_id: str | None = None,
    status: int = 1,
    name: str = "Dr Pool",
    phone: str | None = None,
    user_id: uuid.UUID | None = None,
) -> Doctor:
    """Crea un Doctor ligado a un Profile. Por defecto crea el Profile (con last_seen_at
    reciente/viejo según `online`); si se pasa `user_id`, liga a esa cuenta existente."""
    if user_id is None:
        prof = make_profile(role="doctor")
        prof.last_seen_at = datetime.now(UTC) - (
            timedelta(minutes=1) if online else timedelta(hours=1)
        )
        db.add(prof)
        await db.flush()
        user_id = prof.id
    doctor = Doctor(
        full_name=name,
        user_id=user_id,
        status=status,
        phone=phone,
        specialty_id=uuid.UUID(specialty_id) if specialty_id else None,
        professional_type_id=uuid.UUID(type_id),
    )
    db.add(doctor)
    await db.flush()
    return doctor


async def _pool(client: AsyncClient, **params: object) -> dict:
    resp = await client.get(f"{PREFIX}/doctors/pool", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_pool_shape_and_online_flag(client: AsyncClient, db_session: AsyncSession) -> None:
    nutri = await _type_id(db_session, "nutricionista")
    d_on = await _pool_doctor(db_session, online=True, type_id=nutri, name="Pool Online")
    d_off = await _pool_doctor(db_session, online=False, type_id=nutri, name="Pool Offline")

    body = await _pool(client, professional_type_id=nutri)
    assert set(body) == {"items", "total"}
    assert body["total"] >= 2
    by_id = {i["id"]: i for i in body["items"]}
    assert by_id[str(d_on.id)]["online"] is True
    assert by_id[str(d_off.id)]["online"] is False


async def test_pool_online_tab_filters(client: AsyncClient, db_session: AsyncSession) -> None:
    nutri = await _type_id(db_session, "nutricionista")
    d_on = await _pool_doctor(db_session, online=True, type_id=nutri)
    d_off = await _pool_doctor(db_session, online=False, type_id=nutri)

    online = await _pool(client, professional_type_id=nutri, online=True)
    ids = {i["id"] for i in online["items"]}
    assert str(d_on.id) in ids and str(d_off.id) not in ids

    offline = await _pool(client, professional_type_id=nutri, online=False)
    ids = {i["id"] for i in offline["items"]}
    assert str(d_off.id) in ids and str(d_on.id) not in ids


async def test_pool_specialty_filter(client: AsyncClient, db_session: AsyncSession) -> None:
    nutri = await _type_id(db_session, "nutricionista")
    specs = (await db_session.execute(select(Specialty).limit(2))).scalars().all()
    assert len(specs) == 2
    d_s1 = await _pool_doctor(
        db_session, online=True, type_id=nutri, specialty_id=str(specs[0].id)
    )
    d_s2 = await _pool_doctor(
        db_session, online=True, type_id=nutri, specialty_id=str(specs[1].id)
    )

    body = await _pool(client, professional_type_id=nutri, specialty_id=str(specs[0].id))
    ids = {i["id"] for i in body["items"]}
    assert str(d_s1.id) in ids and str(d_s2.id) not in ids


async def test_pool_excludes_revoked(client: AsyncClient, db_session: AsyncSession) -> None:
    nutri = await _type_id(db_session, "nutricionista")
    d_active = await _pool_doctor(db_session, online=True, type_id=nutri, status=1)
    d_baja = await _pool_doctor(db_session, online=True, type_id=nutri, status=0)
    d_expelled = await _pool_doctor(db_session, online=True, type_id=nutri, status=2)

    body = await _pool(client, professional_type_id=nutri)
    ids = {i["id"] for i in body["items"]}
    assert str(d_active.id) in ids
    assert str(d_baja.id) not in ids and str(d_expelled.id) not in ids


async def test_pool_requires_doctors_read(client: AsyncClient, db_session: AsyncSession) -> None:
    patient = make_profile(role="patient")  # patient no tiene 'doctors.read'
    db_session.add(patient)
    await db_session.flush()
    resp = await client.get(f"{PREFIX}/doctors/pool", headers=auth_headers(patient.id))
    assert resp.status_code == 403


async def test_pool_excludes_self(
    client: AsyncClient, db_session: AsyncSession, admin_identity
) -> None:
    """El médico que consulta (principal = admin_identity del client) no aparece en su pool."""
    nutri = await _type_id(db_session, "nutricionista")
    mine = await _pool_doctor(db_session, online=True, type_id=nutri, user_id=admin_identity.id)
    other = await _pool_doctor(db_session, online=True, type_id=nutri)

    body = await _pool(client, professional_type_id=nutri)
    ids = {i["id"] for i in body["items"]}
    assert str(mine.id) not in ids
    assert str(other.id) in ids


async def test_pool_returns_phone(client: AsyncClient, db_session: AsyncSession) -> None:
    nutri = await _type_id(db_session, "nutricionista")
    doc = await _pool_doctor(db_session, online=True, type_id=nutri, phone="+584145200715")
    body = await _pool(client, professional_type_id=nutri)
    item = next(i for i in body["items"] if i["id"] == str(doc.id))
    assert item["phone"] == "+584145200715"
