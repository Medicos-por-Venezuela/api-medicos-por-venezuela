"""Sala de espera del paciente en vivo (R5 de tasks/cola-por-especialidad/spec.md).

El paciente solo ve la sala cuando un médico tomó su caso; si lo derivaron, la sala de espera
sigue al caso nuevo. El JSON se prueba por endpoint; el stream SSE, como unidad (httpx
`ASGITransport` junta todo el cuerpo antes de devolverlo, así que no puede observar eventos que
llegan con el tiempo) más una prueba de extremo a extremo con un stream que termina enseguida.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import consultation_token
from src.core.config import settings
from src.core.errors import NotFoundError
from src.db.session import get_session_factory
from src.main import app
from src.models.consultation import Consultation
from src.models.patient import Patient
from src.schemas.consultation import WaitingRoomResponse
from src.services import waiting_room
from tests._helpers import GENERAL, add_doctor, auth_headers, make_profile, specialty_id_by_name

PREFIX = "/api/v1"


async def _case(db_session: AsyncSession, *, user_id=None) -> tuple[Consultation, dict]:
    patient = Patient(
        full_name="Ana María Rojas",
        phone_whatsapp="+584140000333",
        email="ana@example.com",
        affected_zone="Caracas",
        consent=True,
        user_id=user_id,
    )
    db_session.add(patient)
    await db_session.flush()
    consultation = Consultation(
        code=f"TEST-{uuid.uuid4().hex[:10]}",
        patient_id=patient.id,
        specialty_id=await specialty_id_by_name(db_session, GENERAL),
        status="waiting",
        # Sala ya creada al registrarse (casos anteriores a este cambio): igual no se muestra.
        video_room_url="https://meet.medicosporvenezuela.org/vamed-previa",
    )
    db_session.add(consultation)
    await db_session.flush()
    return consultation, {"X-Consultation-Token": consultation_token.issue(consultation.id)}


async def test_en_cola_no_se_muestra_la_sala(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Es el bug: el paciente entraba a una sala donde todavía no había nadie."""
    caso, token = await _case(db_session)

    resp = await anon_client.get(f"{PREFIX}/consultations/{caso.id}/waiting-room", headers=token)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "waiting"
    assert body["video_room_url"] is None
    assert body["doctor_name"] is None
    assert body["specialty"] == GENERAL
    assert body["patient_first_name"] == "Ana"
    assert body["access_token"] is None


async def test_cuando_un_medico_lo_toma_aparece_la_sala(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    caso, token = await _case(db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    took = await client.post(
        f"{PREFIX}/consultations/{caso.id}/claim", headers=auth_headers(doc.id)
    )
    assert took.status_code == 200, took.text
    await db_session.refresh(caso)

    body = (
        await anon_client.get(f"{PREFIX}/consultations/{caso.id}/waiting-room", headers=token)
    ).json()
    assert body["phase"] == "ready"
    assert body["video_room_url"] == "https://meet.medicosporvenezuela.org/vamed-previa"
    assert body["doctor_name"] == doc.full_name


async def test_sin_token_o_con_el_de_otro_caso_es_401(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    caso, _ = await _case(db_session)
    _, ajeno = await _case(db_session)

    sin = await anon_client.get(f"{PREFIX}/consultations/{caso.id}/waiting-room")
    otro = await anon_client.get(f"{PREFIX}/consultations/{caso.id}/waiting-room", headers=ajeno)
    assert sin.status_code == 401
    assert otro.status_code == 401


async def test_el_paciente_con_sesion_no_necesita_token(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`/mi-caso` entra con la sesión del paciente dueño."""
    cuenta = make_profile(role="patient")
    db_session.add(cuenta)
    await db_session.flush()
    caso, _ = await _case(db_session, user_id=cuenta.id)

    resp = await anon_client.get(
        f"{PREFIX}/consultations/{caso.id}/waiting-room", headers=auth_headers(cuenta.id)
    )
    assert resp.status_code == 200, resp.text


async def test_si_lo_derivan_sigue_al_caso_nuevo_con_un_token_para_el(
    client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """El paciente conserva el enlace del registro; la sala de espera debe llevarlo a la cola
    del especialista, y el token que recibe tiene que servirle en el caso nuevo."""
    caso, token = await _case(db_session)
    doc = await add_doctor(db_session, specialty=GENERAL)
    await add_doctor(db_session, specialty="Traumatología y ortopedia")
    await client.post(f"{PREFIX}/consultations/{caso.id}/claim", headers=auth_headers(doc.id))
    trauma_id = await specialty_id_by_name(db_session, "Traumatología y ortopedia")
    derivado = await client.post(
        f"{PREFIX}/consultations/{caso.id}/refer-to-queue",
        json={"specialty_id": str(trauma_id), "reason": "Rodilla"},
        headers=auth_headers(doc.id),
    )
    assert derivado.status_code == 201, derivado.text
    hija_id = derivado.json()["id"]

    body = (
        await anon_client.get(f"{PREFIX}/consultations/{caso.id}/waiting-room", headers=token)
    ).json()
    assert body["consultation_id"] == hija_id
    assert body["phase"] == "waiting"
    assert body["specialty"] == "Traumatología y ortopedia"
    assert body["derived_from_specialty"] == GENERAL
    assert body["video_room_url"] is None
    assert consultation_token.is_valid_for(body["access_token"], uuid.UUID(hija_id))

    # Con ese token el paciente puede operar sobre el caso nuevo.
    entered = await anon_client.post(
        f"{PREFIX}/consultations/{hija_id}/entered-call",
        headers={"X-Consultation-Token": body["access_token"]},
    )
    assert entered.status_code == 200, entered.text


async def test_un_caso_cerrado_esta_terminado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    caso, token = await _case(db_session)
    await client.post(f"{PREFIX}/consultations/{caso.id}/close", json={"outcome": "closed"})
    await db_session.refresh(caso)

    body = (
        await client.get(f"{PREFIX}/consultations/{caso.id}/waiting-room", headers=token)
    ).json()
    assert body["phase"] == "finished"
    assert body["video_room_url"] is None


def test_fase_de_un_caso_tomado_sin_sala_es_espera() -> None:
    """Un caso legacy tomado sin sala: el paciente espera, no ve un botón que no lleva a nada."""
    caso = Consultation(status="in_progress", assigned_doctor_id=uuid.uuid4(), video_room_url=None)
    assert waiting_room.phase_of(caso) == waiting_room.PHASE_WAITING
    assert waiting_room.phase_of(Consultation(status="scheduled")) == waiting_room.PHASE_SCHEDULED


# --- Stream SSE --------------------------------------------------------------------------


def _state(phase: str, consultation_id: uuid.UUID, **extra) -> WaitingRoomResponse:
    return WaitingRoomResponse(
        consultation_id=consultation_id, code="CONS-1", status="waiting", phase=phase, **extra
    )


class _Reloj:
    def __init__(self) -> None:
        self.ahora = 0.0

    def __call__(self) -> float:
        return self.ahora

    async def dormir(self, segundos: float) -> None:
        self.ahora += segundos


async def _collect(events) -> list[str]:
    return [e async for e in events]


async def test_sse_emite_al_conectar_en_cada_cambio_y_late_mientras_tanto() -> None:
    cid = uuid.uuid4()
    secuencia = iter(
        [_state("waiting", cid)] * 6
        + [_state("ready", cid, video_room_url="https://x/vamed-1", doctor_name="Dra. Luz")]
        + [_state("ready", cid, video_room_url="https://x/vamed-1", doctor_name="Dra. Luz")] * 10
    )
    reloj = _Reloj()

    async def fetch() -> WaitingRoomResponse:
        return next(secuencia)

    eventos = await _collect(
        waiting_room.sse_events(
            fetch,
            requested_id=cid,
            poll_seconds=4,
            heartbeat_seconds=15,
            max_seconds=60,
            clock=reloj,
            sleep=reloj.dormir,
        )
    )

    status = [e for e in eventos if e.startswith("event: status")]
    assert len(status) == 2  # al conectar (waiting) y cuando cambió (ready)
    assert '"phase":"waiting"' in status[0]
    assert '"phase":"ready"' in status[1] and "vamed-1" in status[1]
    assert ": ping\n\n" in eventos  # 24 s en espera sin cambios: hubo latido
    assert eventos[0].startswith("retry:")


async def test_sse_termina_cuando_el_caso_termina() -> None:
    cid = uuid.uuid4()
    llamadas = 0

    async def fetch() -> WaitingRoomResponse:
        nonlocal llamadas
        llamadas += 1
        return _state("finished", cid)

    reloj = _Reloj()
    eventos = await _collect(
        waiting_room.sse_events(
            fetch,
            requested_id=cid,
            poll_seconds=4,
            heartbeat_seconds=15,
            max_seconds=600,
            clock=reloj,
            sleep=reloj.dormir,
        )
    )
    assert llamadas == 1
    assert '"phase":"finished"' in eventos[-1]


async def test_sse_avisa_si_el_caso_desaparece_y_corta_si_falla_la_base() -> None:
    cid = uuid.uuid4()

    async def borrado() -> WaitingRoomResponse:
        raise NotFoundError("Consulta no encontrada.")

    async def roto() -> WaitingRoomResponse:
        raise RuntimeError("base caída")

    reloj = _Reloj()
    kwargs = dict(
        requested_id=cid,
        poll_seconds=4,
        heartbeat_seconds=15,
        max_seconds=60,
        clock=reloj,
        sleep=reloj.dormir,
    )
    assert (await _collect(waiting_room.sse_events(borrado, **kwargs)))[-1].startswith(
        "event: gone"
    )
    assert await _collect(waiting_room.sse_events(roto, **kwargs)) == ["retry: 5000\n\n"]


async def test_sse_adjunta_token_si_el_caso_vigente_es_otro() -> None:
    pedido, vigente = uuid.uuid4(), uuid.uuid4()

    async def fetch() -> WaitingRoomResponse:
        return _state("finished", vigente)

    reloj = _Reloj()
    eventos = await _collect(
        waiting_room.sse_events(
            fetch,
            requested_id=pedido,
            poll_seconds=4,
            heartbeat_seconds=15,
            max_seconds=60,
            clock=reloj,
            sleep=reloj.dormir,
        )
    )
    assert '"access_token":"' in eventos[-1]


@pytest.fixture
def factory_de_prueba(db_session: AsyncSession) -> AsyncGenerator[None, None]:
    """Las sesiones cortas del stream ven los datos de la transacción de la prueba."""

    @asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[get_session_factory] = lambda: _session
    yield
    app.dependency_overrides.pop(get_session_factory, None)


async def test_stream_de_extremo_a_extremo(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    factory_de_prueba: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WAITING_ROOM_STREAM_MAX_SECONDS", 0)
    caso, token = await _case(db_session)

    resp = await anon_client.get(
        f"{PREFIX}/consultations/{caso.id}/waiting-room/stream", headers=token
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert "event: status" in resp.text
    assert '"phase":"waiting"' in resp.text


async def test_stream_sin_credencial_es_401_y_de_un_caso_inexistente_404(
    anon_client: AsyncClient, db_session: AsyncSession, factory_de_prueba: None
) -> None:
    caso, _ = await _case(db_session)
    sin = await anon_client.get(f"{PREFIX}/consultations/{caso.id}/waiting-room/stream")
    assert sin.status_code == 401

    inexistente = uuid.uuid4()
    resp = await anon_client.get(
        f"{PREFIX}/consultations/{inexistente}/waiting-room/stream",
        headers={"X-Consultation-Token": consultation_token.issue(inexistente)},
    )
    assert resp.status_code == 404


async def test_stream_con_la_sesion_del_paciente(
    anon_client: AsyncClient,
    db_session: AsyncSession,
    factory_de_prueba: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WAITING_ROOM_STREAM_MAX_SECONDS", 0)
    cuenta = make_profile(role="patient")
    db_session.add(cuenta)
    await db_session.flush()
    caso, _ = await _case(db_session, user_id=cuenta.id)

    resp = await anon_client.get(
        f"{PREFIX}/consultations/{caso.id}/waiting-room/stream", headers=auth_headers(cuenta.id)
    )
    assert resp.status_code == 200, resp.text
