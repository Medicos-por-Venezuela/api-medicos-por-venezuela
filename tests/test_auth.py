"""Pruebas de autenticación (JWT de Supabase) y autorización (RBAC + IDOR)."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.profile import Profile
from src.models.rbac import Role, UserRole
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


async def test_no_token_is_401(live_client: AsyncClient) -> None:
    resp = await live_client.get(f"{PREFIX}/queue")
    assert resp.status_code == 401


async def test_invalid_token_is_401(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/queue", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


async def test_patient_role_forbidden_on_staff_endpoint(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patient = make_profile(role="patient")
    db_session.add(patient)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/queue", headers=auth_headers(patient.id))
    assert resp.status_code == 403


async def test_doctor_forbidden_on_admin_endpoint(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doctor = make_profile(role="doctor", specialty="Cardiología")
    db_session.add(doctor)
    await db_session.flush()

    # Listar perfiles es admin-only.
    resp = await client.get(f"{PREFIX}/profiles", headers=auth_headers(doctor.id))
    assert resp.status_code == 403


async def test_auth_me(client: AsyncClient, admin_identity: Profile) -> None:
    resp = await client.get(f"{PREFIX}/auth/me")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(admin_identity.id)
    assert resp.json()["role"] == "admin"
    assert "admin" in resp.json()["roles"]


async def test_auth_me_dual_expone_rol_efectivo_super_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """La fuente de verdad del rol es user_roles (RBAC), no la columna legada users.role:
    un dual con rol principal 'doctor' + super_admin asignado después debe presentarse como
    super_admin en /auth/me, con la lista completa en `roles`."""
    dual = make_profile(role="doctor")  # el trigger de sync agrega 'doctor' a user_roles
    db_session.add(dual)
    await db_session.flush()
    # super_admin ADICIONAL directo en user_roles (otorgarlo vía API exige un super_admin
    # caller y el client del fixture es admin — aquí probamos /auth/me, no el otorgamiento).
    super_admin_id = (
        await db_session.execute(select(Role.id).where(Role.code == "super_admin"))
    ).scalar_one()
    db_session.add(UserRole(user_id=dual.id, role_id=super_admin_id))
    await db_session.flush()

    body = (await client.get(f"{PREFIX}/auth/me", headers=auth_headers(dual.id))).json()
    assert body["role"] == "super_admin"  # el efectivo más alto, no el legado 'doctor'
    assert set(body["roles"]) >= {"doctor", "super_admin"}


async def test_auth_me_admin_puro_no_es_medico(
    client: AsyncClient, admin_identity: Profile
) -> None:
    """Admin sin ficha de médico: has_doctor_profile=False → el panel NO lo redirige a perfil."""
    body = (await client.get(f"{PREFIX}/auth/me")).json()
    assert body["has_doctor_profile"] is False
    assert body["doctor_cedula"] is None


async def test_auth_me_medico_sin_ficha_incluye_contexto(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Médico de Google (rol doctor, sin fila en doctors): has_doctor_profile=True y cédula None,
    resuelto en la MISMA llamada de /auth/me → el panel lo manda a completar cédula."""
    doctor = make_profile(role="doctor")
    db_session.add(doctor)
    await db_session.flush()
    body = (await client.get(f"{PREFIX}/auth/me", headers=auth_headers(doctor.id))).json()
    assert body["has_doctor_profile"] is True
    assert body["doctor_cedula"] is None


async def test_auth_me_permissions(client: AsyncClient, admin_identity: Profile) -> None:
    resp = await client.get(f"{PREFIX}/auth/me/permissions")
    assert resp.status_code == 200
    body = resp.json()
    assert "admin" in body["roles"]
    assert "users.create" in body["permissions"]
    assert "roles.assign" in body["permissions"]
    # Ordenado y sin duplicados: útil para snapshots estables en el frontend.
    assert body["roles"] == sorted(set(body["roles"]))
    assert body["permissions"] == sorted(set(body["permissions"]))


async def test_auth_me_permissions_requires_token(live_client: AsyncClient) -> None:
    resp = await live_client.get(f"{PREFIX}/auth/me/permissions")
    assert resp.status_code == 401


async def test_revoked_doctor_loses_staff_access(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doctor = make_profile(role="doctor", specialty="Cardiología")
    doctor.active = False  # revocado
    db_session.add(doctor)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/queue", headers=auth_headers(doctor.id))
    assert resp.status_code == 403


async def test_patient_sees_only_own_consultations(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = make_profile(role="patient")
    owner.verified = False
    db_session.add(owner)
    await db_session.flush()

    # Paciente del titular (cuenta vinculada) + su consulta.
    own_patient = (
        await client.post(
            f"{PREFIX}/patients",
            json={
                "full_name": "Dueño",
                "phone_whatsapp": "+58412111111",
                "affected_zone": "Caracas",
                "consent": True,
                "user_id": str(owner.id),
            },
        )
    ).json()["id"]
    own_consult = (
        await client.post(f"{PREFIX}/consultations", json={"patient_id": own_patient})
    ).json()["id"]

    # Consulta de OTRO paciente (sin cuenta).
    other_patient = (
        await client.post(
            f"{PREFIX}/patients",
            json={
                "full_name": "Ajeno",
                "phone_whatsapp": "+58412222222",
                "affected_zone": "Caracas",
                "consent": True,
            },
        )
    ).json()["id"]
    other_consult = (
        await client.post(f"{PREFIX}/consultations", json={"patient_id": other_patient})
    ).json()["id"]

    headers = auth_headers(owner.id)
    listed = await client.get(f"{PREFIX}/consultations", headers=headers)
    ids = {c["id"] for c in listed.json()}
    assert own_consult in ids
    assert other_consult not in ids

    # Acceso directo al ajeno -> 404 (anti-IDOR).
    assert (
        await client.get(f"{PREFIX}/consultations/{other_consult}", headers=headers)
    ).status_code == 404
    assert (
        await client.get(f"{PREFIX}/consultations/{own_consult}", headers=headers)
    ).status_code == 200
