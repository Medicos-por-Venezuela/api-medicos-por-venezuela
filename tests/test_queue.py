"""Pruebas funcionales de la cola (camino feliz y 404), aisladas por savepoint.

El médico que toma el caso es el titular del JWT (el `client` va como admin).
"""

from httpx import AsyncClient

from src.models.profile import Profile

PREFIX = "/api/v1"


async def _waiting_consultation(client: AsyncClient) -> str:
    patient_id = (
        await client.post(
            f"{PREFIX}/patients",
            json={
                "full_name": "Paciente Cola",
                "phone_whatsapp": "+58412777000",
                "affected_zone": "Caracas",
                "consent": True,
            },
        )
    ).json()["id"]
    return (await client.post(f"{PREFIX}/consultations", json={"patient_id": patient_id})).json()[
        "id"
    ]


async def test_queue_list(client: AsyncClient) -> None:
    await _waiting_consultation(client)
    resp = await client.get(f"{PREFIX}/queue")
    assert resp.status_code == 200
    assert all(c["status"] == "waiting" for c in resp.json())


async def test_queue_take_success_then_404(client: AsyncClient, admin_identity: Profile) -> None:
    cid = await _waiting_consultation(client)

    taken = await client.post(f"{PREFIX}/queue/{cid}/take")
    assert taken.status_code == 200, taken.text
    body = taken.json()
    assert body["status"] == "in_progress"
    assert body["assigned_doctor_id"] == str(admin_identity.id)
    assert body["opened_at"] is not None

    # Ya no está en espera -> 404.
    again = await client.post(f"{PREFIX}/queue/{cid}/take")
    assert again.status_code == 404


async def test_queue_take_missing_404(client: AsyncClient) -> None:
    resp = await client.post(f"{PREFIX}/queue/00000000-0000-0000-0000-000000000000/take")
    assert resp.status_code == 404
