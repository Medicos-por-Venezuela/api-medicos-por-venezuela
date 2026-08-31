"""Pruebas de concurrencia de la interconsulta asíncrona.

Objetivo: que dos especialistas NUNCA tomen el mismo caso. El ganador recibe 200 y el perdedor
409, siempre rápido — el modo de fallo que se está evitando no es solo la doble asignación, sino
también que la petición perdedora se quede colgada esperando a que la otra transacción termine.

Usa sesiones y conexiones REALES (sin el override de `get_db`) para que el bloqueo de filas sea
genuino: con una sesión compartida no habría dos transacciones y el lock no probaría nada.
Mismo montaje que `test_queue_concurrency.py`.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from src.db.session import AsyncSessionLocal
from src.models.doctor import Doctor
from src.models.interconsultation_request import InterconsultationRequest
from src.models.patient import Patient
from src.models.profile import Profile
from src.models.specialty import Specialty
from tests._helpers import auth_headers, make_doctor_row, make_profile

PREFIX = "/api/v1"
SOLICITUDES = f"{PREFIX}/interconsultation-requests"


async def _seed() -> dict:
    """Committea: una especialidad, un tratante con su paciente, DOS especialistas de esa
    especialidad y una solicitud abierta.

    Los especialistas llevan ficha habilitada en `doctors`: sin ella el gate de credencial los
    deja sin permisos y la carrera ni siquiera llegaría al lock."""
    async with AsyncSessionLocal() as s:
        specialty = Specialty(name=f"Concurrencia {uuid.uuid4().hex[:8]}")
        s.add(specialty)
        await s.flush()

        tratante = make_profile(role="doctor")
        s.add(tratante)
        await s.flush()
        s.add(make_doctor_row(tratante.id))

        especialistas = []
        for _ in range(2):
            p = make_profile(role="doctor")
            p.specialty_id = specialty.id
            s.add(p)
            await s.flush()
            s.add(make_doctor_row(p.id))
            especialistas.append(p.id)

        paciente = Patient(
            full_name="Paciente Concurrencia",
            consent=True,
            age_range="40-49",
            created_by_doctor_id=tratante.id,
        )
        s.add(paciente)
        await s.flush()

        solicitud = InterconsultationRequest(
            patient_id=paciente.id,
            requesting_doctor_id=tratante.id,
            mode="specialty",
            specialty_id=specialty.id,
            chief_complaint="Caso de prueba de concurrencia.",
        )
        s.add(solicitud)
        await s.commit()
        return {
            "request_id": solicitud.id,
            "patient_id": paciente.id,
            "specialty_id": specialty.id,
            "perfiles": [tratante.id, *especialistas],
            "especialistas": especialistas,
        }


async def _cleanup(datos: dict) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(
            delete(InterconsultationRequest).where(
                InterconsultationRequest.id == datos["request_id"]
            )
        )
        await s.execute(delete(Patient).where(Patient.id == datos["patient_id"]))
        for uid in datos["perfiles"]:
            await s.execute(delete(Doctor).where(Doctor.user_id == uid))
            await s.execute(delete(Profile).where(Profile.id == uid))
        await s.execute(delete(Specialty).where(Specialty.id == datos["specialty_id"]))
        await s.commit()


@pytest_asyncio.fixture
async def caso_abierto() -> AsyncGenerator[dict, None]:
    datos = await _seed()
    try:
        yield datos
    finally:
        await _cleanup(datos)


async def test_fila_bloqueada_devuelve_409_sin_colgarse(
    live_client: AsyncClient, caso_abierto: dict
) -> None:
    """Con la fila ya bloqueada por otra transacción, `take` falla RÁPIDO con 409.

    Es lo que compra `nowait=True`: sin él, esta petición esperaría a que el otro haga commit
    o rollback — y en un panel con médicos esperando, colgarse es peor que perder."""
    request_id = caso_abierto["request_id"]
    especialista = caso_abierto["especialistas"][0]

    tenedor = AsyncSessionLocal()
    conn = await tenedor.connection()
    await conn.execute(
        select(InterconsultationRequest)
        .where(InterconsultationRequest.id == request_id)
        .with_for_update()
    )
    try:
        resp = await asyncio.wait_for(
            live_client.post(
                f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(especialista)
            ),
            timeout=10,
        )
        assert resp.status_code == 409, resp.text
    finally:
        await tenedor.rollback()
        await tenedor.close()


async def test_dos_especialistas_a_la_vez_un_solo_ganador(
    live_client: AsyncClient, caso_abierto: dict
) -> None:
    """Los dos hacen clic en el mismo milisegundo: exactamente un 200 y un 409."""
    request_id = caso_abierto["request_id"]
    uno, dos = caso_abierto["especialistas"]

    async def tomar(doctor_id: uuid.UUID) -> int:
        resp = await live_client.post(
            f"{SOLICITUDES}/{request_id}/take", headers=auth_headers(doctor_id)
        )
        return resp.status_code

    codigos = await asyncio.gather(tomar(uno), tomar(dos))

    assert codigos.count(200) == 1, f"Debe haber exactamente un ganador: {codigos}"
    assert codigos.count(409) == 1, f"Y exactamente un perdedor con 409: {codigos}"

    async with AsyncSessionLocal() as s:
        fila = await s.get(InterconsultationRequest, request_id)
        assert fila.status == "taken"
        assert fila.taken_by_doctor_id in (uno, dos)
        assert fila.taken_at is not None


async def test_cinco_a_la_vez_siguen_dando_un_solo_ganador(
    live_client: AsyncClient, caso_abierto: dict
) -> None:
    """Más presión sobre la misma fila: el número de perdedores cambia, el de ganadores no."""
    request_id = caso_abierto["request_id"]
    especialista = caso_abierto["especialistas"][0]
    headers = auth_headers(especialista)

    async def tomar() -> int:
        resp = await live_client.post(f"{SOLICITUDES}/{request_id}/take", headers=headers)
        return resp.status_code

    codigos = await asyncio.gather(*[tomar() for _ in range(5)])

    assert codigos.count(200) == 1, f"Debe haber exactamente un ganador: {codigos}"
    assert all(c in (200, 409) for c in codigos), codigos

    async with AsyncSessionLocal() as s:
        fila = await s.get(InterconsultationRequest, request_id)
        assert fila.taken_by_doctor_id == especialista
