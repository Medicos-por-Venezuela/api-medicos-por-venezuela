"""Pruebas de integración de la lógica de negocio portada del Next.js:
attend-next, cierre/no-show, heartbeat, sala de video y acciones de perfil.

El `client` va autenticado como admin; cuando se necesita un médico con especialidad
concreta (matching), se firma un JWT para un perfil doctor insertado en la sesión.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.profile import Profile
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


async def _patient(client: AsyncClient, needs: list[str]) -> str:
    return (
        await client.post(
            f"{PREFIX}/patients",
            json={
                "full_name": "Paciente Negocio",
                "phone_whatsapp": "+58412800000",
                "affected_zone": "Caracas",
                "needs_tags": needs,
                "consent": True,
            },
        )
    ).json()["id"]


async def _consultation(client: AsyncClient, needs: list[str]) -> str:
    pid = await _patient(client, needs)
    return (await client.post(f"{PREFIX}/consultations", json={"patient_id": pid})).json()["id"]


async def _doctor(db_session: AsyncSession, specialty: str) -> Profile:
    doc = make_profile(role="doctor", specialty=specialty)
    db_session.add(doc)
    await db_session.flush()
    return doc


# --- attend-next ---


async def test_attend_next_picks_specialty_match(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await _doctor(db_session, "Traumatología")
    cid = await _consultation(client, ["Lesión física"])  # -> category 'Lesión física'

    resp = await client.post(f"{PREFIX}/queue/attend-next", headers=auth_headers(doc.id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == cid
    assert body["status"] == "in_progress"
    assert body["assigned_doctor_id"] == str(doc.id)
    assert body["priority"] == "review"  # derivado de 'Lesión física'


async def test_attend_next_no_eligible_returns_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    psych = await _doctor(db_session, "Psicología")
    await _consultation(client, ["Lesión física"])  # caso físico
    # Un psicólogo no puede atender un caso físico -> no hay elegibles.
    resp = await client.post(f"{PREFIX}/queue/attend-next", headers=auth_headers(psych.id))
    assert resp.status_code == 404


# --- cierre / no-show ---


async def test_close_consultation_creates_event(client: AsyncClient) -> None:
    cid = await _consultation(client, ["Medicina general"])
    await client.post(f"{PREFIX}/queue/{cid}/take")

    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/close",
        json={"outcome": "closed", "note": "Atendido"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "closed"
    assert resp.json()["closed_at"] is not None

    events = (await client.get(f"{PREFIX}/consultations/{cid}/events")).json()
    assert any(e["event_type"] == "closed" for e in events)


# --- heartbeat (público) ---


async def test_patient_heartbeat(client: AsyncClient) -> None:
    cid = await _consultation(client, ["Medicina general"])
    resp = await client.post(f"{PREFIX}/consultations/{cid}/heartbeat")
    assert resp.status_code == 200
    assert resp.json()["patient_last_seen_at"] is not None


# --- sala de video (idempotente, pública) ---


async def test_video_room_idempotent_and_conflict(client: AsyncClient) -> None:
    cid = await _consultation(client, ["Medicina general"])

    first = await client.post(f"{PREFIX}/consultations/{cid}/video-room")
    assert first.status_code == 200
    url = first.json()["video_room_url"]
    assert url.startswith("https://") and "/vamed-" in url

    # Idempotente: misma URL.
    second = await client.post(f"{PREFIX}/consultations/{cid}/video-room")
    assert second.json()["video_room_url"] == url

    # Una consulta tomada (in_progress) y sin sala -> 409.
    cid2 = await _consultation(client, ["Medicina general"])
    await client.post(f"{PREFIX}/queue/{cid2}/take")
    conflict = await client.post(f"{PREFIX}/consultations/{cid2}/video-room")
    assert conflict.status_code == 409


# --- acciones de perfil ---


async def test_profile_online_and_active(client: AsyncClient, db_session: AsyncSession) -> None:
    # Presencia del médico autenticado (admin es staff).
    online = await client.post(f"{PREFIX}/profiles/me/online")
    assert online.status_code == 200
    assert online.json()["last_seen_at"] is not None

    # Revocar/reactivar a OTRO perfil (no el propio admin, para no perder permisos).
    target = await _doctor(db_session, "Cardiología")
    revoked = await client.patch(f"{PREFIX}/profiles/{target.id}/active", json={"active": False})
    assert revoked.status_code == 200
    assert revoked.json()["active"] is False

    reactivated = await client.patch(
        f"{PREFIX}/profiles/{target.id}/active", json={"active": True}
    )
    assert reactivated.json()["active"] is True

    audit_resp = await client.get(f"{PREFIX}/audit-log", params={"resource": "users"})
    actions = [e["action"] for e in audit_resp.json() if e["resource_id"] == str(target.id)]
    assert sorted(actions) == sorted(["profile.activated", "profile.deactivated"])


async def test_finalize_role_once(client: AsyncClient, db_session: AsyncSession) -> None:
    # Perfil placeholder (role_chosen=False) que finaliza su propio rol vía su JWT.
    placeholder = make_profile(role="patient")
    placeholder.role_chosen = False
    placeholder.verified = False
    db_session.add(placeholder)
    await db_session.flush()

    ok = await client.post(
        f"{PREFIX}/profiles/me/finalize-role",
        headers=auth_headers(placeholder.id),
        json={"role": "doctor", "specialty": "Cardiología", "country": "Venezuela"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["role"] == "doctor"
    assert ok.json()["role_chosen"] is True

    audit_resp = await client.get(f"{PREFIX}/audit-log", params={"action": "profile.role_chosen"})
    entry = next(e for e in audit_resp.json() if e["resource_id"] == str(placeholder.id))
    assert entry["actor_user_id"] == str(placeholder.id)
    assert entry["metadata"]["role"] == "doctor"

    # Segunda vez -> 400 (ya fue elegido).
    again = await client.post(
        f"{PREFIX}/profiles/me/finalize-role",
        headers=auth_headers(placeholder.id),
        json={"role": "patient"},
    )
    assert again.status_code == 400


async def test_finalize_role_rejects_invalid_role(client: AsyncClient) -> None:
    resp = await client.post(f"{PREFIX}/profiles/me/finalize-role", json={"role": "admin"})
    assert resp.status_code == 422  # bloqueado por el patrón del schema
