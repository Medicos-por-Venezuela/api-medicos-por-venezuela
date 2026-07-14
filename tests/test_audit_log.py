"""Pruebas del endpoint de lectura del audit_log."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy import update as sa_update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import AsyncSessionLocal
from src.models.audit_log import AuditLog
from src.models.profile import Profile
from src.services import audit
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


async def _waiting_consultation(client: AsyncClient) -> str:
    patient_id = (
        await client.post(
            f"{PREFIX}/patients",
            json={
                "full_name": "Paciente Audit",
                "phone_whatsapp": "+58412888000",
                "affected_zone": "Caracas",
                "consent": True,
            },
        )
    ).json()["id"]
    return (await client.post(f"{PREFIX}/consultations", json={"patient_id": patient_id})).json()[
        "id"
    ]


async def test_audit_log_muestra_asignaciones(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = make_profile(role="patient")
    db_session.add(user)
    await db_session.flush()
    await client.post(f"{PREFIX}/users/{user.id}/roles", json={"role_code": "doctor"})

    resp = await client.get(f"{PREFIX}/audit-log", params={"action": "role.assigned"})
    assert resp.status_code == 200
    entry = next(e for e in resp.json() if e["resource_id"] == str(user.id))
    assert entry["action"] == "role.assigned"
    assert entry["metadata"]["role"] == "doctor"  # se expone como "metadata"


async def test_audit_log_requiere_permiso(client: AsyncClient, db_session: AsyncSession) -> None:
    doctor = make_profile(role="doctor")  # doctor no tiene 'audit.read'
    db_session.add(doctor)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/audit-log", headers=auth_headers(doctor.id))
    assert resp.status_code == 403


async def test_audit_log_sin_token_401(live_client: AsyncClient) -> None:
    resp = await live_client.get(f"{PREFIX}/audit-log")
    assert resp.status_code == 401


async def test_audit_log_registra_cierre_de_consulta(client: AsyncClient) -> None:
    cid = await _waiting_consultation(client)
    await client.post(f"{PREFIX}/queue/{cid}/take")
    await client.post(f"{PREFIX}/consultations/{cid}/close", json={"outcome": "closed"})

    resp = await client.get(f"{PREFIX}/audit-log", params={"action": "consultation.closed"})
    assert resp.status_code == 200
    entry = next(e for e in resp.json() if e["resource_id"] == cid)
    assert entry["metadata"]["outcome"] == "closed"


async def test_audit_log_registra_borrado_de_consulta(
    client: AsyncClient, admin_identity: Profile
) -> None:
    cid = await _waiting_consultation(client)
    await client.delete(f"{PREFIX}/consultations/{cid}")

    resp = await client.get(f"{PREFIX}/audit-log", params={"action": "consultation.deleted"})
    assert resp.status_code == 200
    entry = next(e for e in resp.json() if e["resource_id"] == cid)
    assert entry["actor_user_id"] == str(admin_identity.id)


async def test_audit_log_actor_se_pone_null_al_borrar_el_perfil() -> None:
    """El trigger de inmutabilidad debe permitir el ON DELETE SET NULL de su propia
    FK a profiles (único UPDATE permitido); borrar el actor de una acción auditada
    no debe fallar. Usa una transacción real (commit, no el savepoint de `db_session`)
    porque el bug solo aparece con un DELETE de verdad. El audit_log resultante no se
    limpia al final -- es append-only por diseño, ni este test podría borrarlo."""
    async with AsyncSessionLocal() as s:
        actor = make_profile(role="doctor")
        s.add(actor)
        await s.flush()
        entry = await audit.log_action(
            s, action="doctor.updated", actor_user_id=actor.id, resource="doctors"
        )
        await s.commit()
        entry_id, actor_id = entry.id, actor.id

    async with AsyncSessionLocal() as s:
        await s.delete(await s.get(Profile, actor_id))
        await s.commit()  # no debe lanzar "audit_log es inmutable"

    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(AuditLog).where(AuditLog.id == entry_id))).scalar_one()
        assert row.actor_user_id is None


async def test_audit_log_update_manual_de_actor_rechazado() -> None:
    """Endurecimiento del trigger: un UPDATE manual que anonimice actor_user_id
    (sin pasar por el ON DELETE SET NULL del FK) debe rechazarse — solo el trigger
    RI interno (pg_trigger_depth > 1) puede ponerlo en NULL."""
    async with AsyncSessionLocal() as s:
        actor = make_profile(role="doctor")
        s.add(actor)
        await s.flush()
        entry = await audit.log_action(
            s, action="doctor.updated", actor_user_id=actor.id, resource="doctors"
        )
        await s.commit()
        entry_id = entry.id

    with pytest.raises(DBAPIError, match="inmutable"):
        async with AsyncSessionLocal() as s:
            await s.execute(
                sa_update(AuditLog).where(AuditLog.id == entry_id).values(actor_user_id=None)
            )
            await s.commit()

    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(AuditLog).where(AuditLog.id == entry_id))).scalar_one()
        assert row.actor_user_id is not None  # sigue intacto


async def test_audit_log_truncate_rechazado() -> None:
    """TRUNCATE no dispara triggers de fila (era el último vector para vaciar la bitácora);
    el trigger de sentencia trg_audit_log_no_truncate debe bloquearlo."""
    with pytest.raises(DBAPIError, match="inmutable"):
        async with AsyncSessionLocal() as s:
            await s.execute(text("truncate table public.audit_log"))
            await s.commit()
