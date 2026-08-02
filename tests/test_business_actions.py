"""Pruebas de integración de la lógica de negocio portada del Next.js:
separación por especialidad (cola + claim), cierre/no-show, sala de video y acciones de perfil.

El `client` va autenticado como admin; cuando se necesita un médico con especialidad
concreta (matching), se firma un JWT para un perfil doctor insertado en la sesión.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.profile import Profile
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"


async def _patient(client: AsyncClient, needs: list[str], allergies: str | None = None) -> str:
    return (
        await client.post(
            f"{PREFIX}/patients",
            json={
                "full_name": "Paciente Negocio",
                "phone_whatsapp": "+58412800000",
                "affected_zone": "Caracas",
                "needs_tags": needs,
                "consent": True,
                "allergies": allergies,
            },
        )
    ).json()["id"]


async def _consultation(client: AsyncClient, needs: list[str]) -> str:
    pid = await _patient(client, needs)
    return (await client.post(f"{PREFIX}/consultations", json={"patient_id": pid})).json()["id"]


async def _consultation_and_room_headers(
    client: AsyncClient, needs: list[str]
) -> tuple[str, dict[str, str]]:
    """Consulta + la cabecera con su token de sala, como la recibe el paciente al registrarse."""
    pid = await _patient(client, needs)
    body = (await client.post(f"{PREFIX}/consultations", json={"patient_id": pid})).json()
    return body["id"], {"X-Consultation-Token": body["access_token"]}


async def _doctor(db_session: AsyncSession, specialty: str, role: str = "doctor") -> Profile:
    doc = make_profile(role=role, specialty=specialty)
    db_session.add(doc)
    await db_session.flush()
    return doc


async def _specialty_id(client: AsyncClient, name: str) -> str:
    specs = (await client.get(f"{PREFIX}/specialties")).json()
    return next(s["id"] for s in specs if s["name"] == name)


async def _consultation_with_specialty(
    client: AsyncClient, needs: list[str], specialty_name: str
) -> str:
    """Consulta con especialidad EXPLÍCITA (specialty_id), como las crea el registro nuevo."""
    pid = await _patient(client, needs)
    sid = await _specialty_id(client, specialty_name)
    return (
        await client.post(
            f"{PREFIX}/consultations", json={"patient_id": pid, "specialty_id": sid}
        )
    ).json()["id"]


# --- Separación por especialidad en la cola y en el claim ---
# Antes esto se probaba contra POST /queue/attend-next, un endpoint que ningún cliente
# llamaba. La regla vive ahora donde el panel la ejerce de verdad: GET /consultations/panel
# (qué casos VE el médico) y POST /consultations/{id}/claim (cuáles puede TOMAR).


async def _panel_waiting_ids(client: AsyncClient, doctor_id) -> list[str]:
    resp = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doctor_id))
    assert resp.status_code == 200, resp.text
    return [c["id"] for c in resp.json()["waiting"]]


async def test_claim_asigna_caso_de_su_especialidad(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await _doctor(db_session, "Traumatología")
    cid = await _consultation(client, ["Lesión física"])  # -> category 'Lesión física'

    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(doc.id)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == cid
    assert body["status"] == "in_progress"
    assert body["assigned_doctor_id"] == str(doc.id)


async def test_psicologo_no_ve_caso_fisico_en_la_cola(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El bug reportado en producción: el panel devolvía TODA la cola y un psicólogo veía la
    cédula, el teléfono y el motivo de un caso de salud física."""
    psych = await _doctor(db_session, "Psicología")
    cid = await _consultation(client, ["Lesión física"])

    assert cid not in await _panel_waiting_ids(client, psych.id)


async def test_psicologo_no_puede_tomar_caso_fisico_ni_por_post_directo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Filtrar la lista no es control de acceso: el claim debe rechazarlo por su cuenta,
    porque un POST directo se salta cualquier filtro de la UI."""
    psych = await _doctor(db_session, "Psicología")
    cid = await _consultation(client, ["Lesión física"])

    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/claim", json={}, headers=auth_headers(psych.id)
    )
    assert resp.status_code == 403, resp.text


async def test_caso_de_psicologia_reservado_por_specialty_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un caso con especialidad Psicología solo va a Psicología/Psiquiatría, aunque sus
    needs_tags sean genéricos (la especialidad explícita manda sobre el fallback legacy).
    La reserva es bidireccional: el general ni lo ve ni lo puede tomar."""
    cid_psi = await _consultation_with_specialty(client, ["Medicina general"], "Psicología")

    general = await _doctor(db_session, "Medicina general")
    assert cid_psi not in await _panel_waiting_ids(client, general.id)
    blocked = await client.post(
        f"{PREFIX}/consultations/{cid_psi}/claim", json={}, headers=auth_headers(general.id)
    )
    assert blocked.status_code == 403, blocked.text

    psych = await _doctor(db_session, "Psicología")
    assert cid_psi in await _panel_waiting_ids(client, psych.id)
    took = await client.post(
        f"{PREFIX}/consultations/{cid_psi}/claim", json={}, headers=auth_headers(psych.id)
    )
    assert took.status_code == 200, took.text
    assert took.json()["id"] == cid_psi


async def test_admin_ve_y_toma_toda_la_cola(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El admin no queda acotado por especialidad: sigue viendo la cola entera."""
    cid_psi = await _consultation_with_specialty(client, ["Medicina general"], "Psicología")
    admin = await _doctor(db_session, "Medicina general", role="admin")

    assert cid_psi in await _panel_waiting_ids(client, admin.id)
    took = await client.post(
        f"{PREFIX}/consultations/{cid_psi}/claim", json={}, headers=auth_headers(admin.id)
    )
    assert took.status_code == 200, took.text


async def test_la_cola_del_panel_expone_las_alergias(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Las alergias se piden en el registro y son dato de decisión clínica: el médico las
    necesita ANTES de tomar el caso."""
    doc = await _doctor(db_session, "Medicina general")
    pid = await _patient(client, ["Medicina general"], allergies="Penicilina")
    cid = (
        await client.post(f"{PREFIX}/consultations", json={"patient_id": pid})
    ).json()["id"]

    resp = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(doc.id))
    assert resp.status_code == 200, resp.text
    row = next(c for c in resp.json()["waiting"] if c["id"] == cid)
    assert row["patient"]["allergies"] == "Penicilina"
    # Sigue SIN nombre: las alergias se suman al card anónimo, no lo destapan.
    assert "full_name" not in row["patient"]


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


# El test del heartbeat se eliminó con el endpoint: era el único escritor de
# `patient_last_seen_at` y no lo llamaba ningún cliente. La presencia del paciente la
# resuelve Realtime Presence, que se cubre en los e2e (paciente-en-linea, presence).


# --- entered-call (idempotente, sin sesión pero con token de sala) ---


async def test_mark_entered_call_sets_once(client: AsyncClient) -> None:
    cid, room = await _consultation_and_room_headers(client, ["Medicina general"])
    resp = await client.post(f"{PREFIX}/consultations/{cid}/entered-call", headers=room)
    assert resp.status_code == 200
    first = resp.json()["entered_call_at"]
    assert first is not None

    # Idempotente: una segunda llamada no cambia entered_call_at.
    resp2 = await client.post(f"{PREFIX}/consultations/{cid}/entered-call", headers=room)
    assert resp2.status_code == 200
    assert resp2.json()["entered_call_at"] == first


# --- sala de video (idempotente, sin sesión pero con token de sala) ---


async def test_video_room_idempotent_and_conflict(client: AsyncClient) -> None:
    cid, room = await _consultation_and_room_headers(client, ["Medicina general"])

    first = await client.post(f"{PREFIX}/consultations/{cid}/video-room", headers=room)
    assert first.status_code == 200
    url = first.json()["video_room_url"]
    assert url.startswith("https://") and "/vamed-" in url

    # Idempotente: misma URL.
    second = await client.post(f"{PREFIX}/consultations/{cid}/video-room", headers=room)
    assert second.json()["video_room_url"] == url

    # Una consulta tomada (in_progress) y sin sala -> 409.
    cid2, room2 = await _consultation_and_room_headers(client, ["Medicina general"])
    await client.post(f"{PREFIX}/queue/{cid2}/take")
    conflict = await client.post(f"{PREFIX}/consultations/{cid2}/video-room", headers=room2)
    assert conflict.status_code == 409


# --- Token de acceso a la sala (hallazgo M3) ---


async def test_sala_sin_token_responde_401(client: AsyncClient, anon_client: AsyncClient) -> None:
    """Conocer el id de la consulta ya no basta para pedir la sala: ESE era el hallazgo.
    Va con `anon_client` porque `client` es admin y entraría por la puerta de staff."""
    cid, _ = await _consultation_and_room_headers(client, ["Medicina general"])

    sala = await anon_client.post(f"{PREFIX}/consultations/{cid}/video-room")
    entrada = await anon_client.post(f"{PREFIX}/consultations/{cid}/entered-call")
    assert sala.status_code == 401
    assert entrada.status_code == 401


async def test_token_de_otra_consulta_no_sirve(
    client: AsyncClient, anon_client: AsyncClient
) -> None:
    """Anti-IDOR con credencial legítima: el token va atado a SU consulta por el claim `sub`.
    Sin esa comprobación, cualquier paciente abriría la sala de cualquier otro."""
    _, room_a = await _consultation_and_room_headers(client, ["Medicina general"])
    cid_b, _ = await _consultation_and_room_headers(client, ["Medicina general"])

    resp = await anon_client.post(f"{PREFIX}/consultations/{cid_b}/video-room", headers=room_a)
    assert resp.status_code == 401


async def test_token_expirado_no_sirve(
    client: AsyncClient, anon_client: AsyncClient, monkeypatch
) -> None:
    """La caducidad es el punto entero del cambio: una URL filtrada deja de funcionar."""
    from src.core import consultation_token
    from src.core.config import settings as app_settings

    cid, _ = await _consultation_and_room_headers(client, ["Medicina general"])
    monkeypatch.setattr(app_settings, "CONSULTATION_TOKEN_TTL_HOURS", -1)  # ya nacido caducado
    expired = {"X-Consultation-Token": consultation_token.issue(uuid.UUID(cid))}

    resp = await anon_client.post(f"{PREFIX}/consultations/{cid}/video-room", headers=expired)
    assert resp.status_code == 401


async def test_staff_abre_la_sala_sin_token_de_paciente(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El médico crea la sala desde el panel cuando el caso llegó sin ella, y NO tiene el token
    del paciente. Exigirle uno lo dejaba fuera de la consulta que está atendiendo."""
    doc = await _doctor(db_session, "Medicina general")
    cid, _ = await _consultation_and_room_headers(client, ["Medicina general"])

    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/video-room", headers=auth_headers(doc.id)
    )
    assert resp.status_code == 200, resp.text


async def test_sesion_de_paciente_no_abre_la_sala_de_otro(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """La puerta de staff es SOLO para staff: una cuenta de paciente autenticada no sustituye al
    token, o el atajo se convertiria en el agujero que M3 venía a cerrar."""
    patient_user = await _doctor(db_session, None, role="patient")
    cid, _ = await _consultation_and_room_headers(client, ["Medicina general"])

    resp = await client.post(
        f"{PREFIX}/consultations/{cid}/video-room", headers=auth_headers(patient_user.id)
    )
    assert resp.status_code == 401, resp.text


async def test_cutover_flag_deja_pasar_sin_token(client: AsyncClient, monkeypatch) -> None:
    """Con CONSULTATION_TOKEN_REQUIRED=false el endpoint tolera la ausencia de token: es la
    ventana entre el deploy del backend y el del frontend. Con la bandera en false M3 NO está
    cerrado — por eso el default es true y la bandera es temporal."""
    from src.core.config import settings as app_settings

    cid, _ = await _consultation_and_room_headers(client, ["Medicina general"])
    monkeypatch.setattr(app_settings, "CONSULTATION_TOKEN_REQUIRED", False)

    resp = await client.post(f"{PREFIX}/consultations/{cid}/video-room")
    assert resp.status_code == 200


async def test_el_token_no_se_filtra_en_los_listados(client: AsyncClient) -> None:
    """Solo POST /consultations entrega el token. Si `ConsultationResponse` lo llevara, cada
    listado del panel repartiría credenciales de sala."""
    cid, _ = await _consultation_and_room_headers(client, ["Medicina general"])

    detail = (await client.get(f"{PREFIX}/consultations/{cid}")).json()
    assert "access_token" not in detail


# --- acciones de perfil ---


async def test_profile_active_toggle(client: AsyncClient, db_session: AsyncSession) -> None:
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
