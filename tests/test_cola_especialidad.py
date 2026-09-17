"""Quién ve y quién toma cada caso de la cola (R1 de tasks/cola-por-especialidad/spec.md).

La cola dejó de separar solo "salud mental vs. física": es por especialidad exacta, con los
accesos extra de `specialty_queue_access` (Psiquiatría → Psicología, Medicina interna →
Medicina general), sin cola para quien tiene "Otra" o ninguna especialidad, y con todos los casos
para un admin salvo que sea de salud mental exclusiva (el caso de producción: un psicólogo
super_admin veía Medicina general).
"""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.consultation import Consultation
from src.models.patient import Patient
from src.models.profile import Profile
from tests._helpers import GENERAL, add_doctor, auth_headers, make_profile, specialty_id_by_name

PREFIX = "/api/v1"
TRAUMA = "Traumatología y ortopedia"
PSICOLOGIA = "Psicología"
PSIQUIATRIA = "Psiquiatría"
INTERNA = "Medicina interna"
PEDIATRIA = "Pediatría y subespecialidades"


async def _case(db_session: AsyncSession, specialty: str, **fields) -> Consultation:
    patient = Patient(
        full_name="Paciente Cola",
        phone_whatsapp="+584140000111",
        affected_zone="Caracas",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()
    consultation = Consultation(
        code=f"TEST-{uuid.uuid4().hex[:10]}",
        patient_id=patient.id,
        specialty_id=await specialty_id_by_name(db_session, specialty),
        status="waiting",
        **fields,
    )
    db_session.add(consultation)
    await db_session.flush()
    return consultation


async def _admin(db_session: AsyncSession, role: str, specialty: str | None) -> Profile:
    admin = make_profile(role=role)
    if specialty is not None:
        admin.specialty_id = await specialty_id_by_name(db_session, specialty)
    db_session.add(admin)
    await db_session.flush()
    return admin


async def _panel(client: AsyncClient, user_id) -> dict:
    resp = await client.get(f"{PREFIX}/consultations/panel", headers=auth_headers(user_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _waiting_ids(client: AsyncClient, user_id) -> set[str]:
    return {c["id"] for c in (await _panel(client, user_id))["waiting"]}


async def _claim(client: AsyncClient, cid, user_id) -> int:
    resp = await client.post(f"{PREFIX}/consultations/{cid}/claim", headers=auth_headers(user_id))
    return resp.status_code


async def test_traumatologo_solo_ve_y_toma_su_especialidad(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Antes un traumatólogo veía Medicina general (la regla solo separaba salud mental)."""
    trauma = await add_doctor(db_session, specialty=TRAUMA)
    suyo = await _case(db_session, TRAUMA)
    ajeno = await _case(db_session, GENERAL)

    ids = await _waiting_ids(client, trauma.id)
    assert str(suyo.id) in ids
    assert str(ajeno.id) not in ids
    assert await _claim(client, ajeno.id, trauma.id) == 403
    assert await _claim(client, suyo.id, trauma.id) == 200


async def test_medicina_interna_tambien_ve_medicina_general(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    internista = await add_doctor(db_session, specialty=INTERNA)
    general = await _case(db_session, GENERAL)
    interna = await _case(db_session, INTERNA)
    pediatria = await _case(db_session, PEDIATRIA)

    ids = await _waiting_ids(client, internista.id)
    assert {str(general.id), str(interna.id)} <= ids
    assert str(pediatria.id) not in ids
    assert await _claim(client, general.id, internista.id) == 200


async def test_el_acceso_extra_no_es_reciproco(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Medicina interna ve Medicina general, pero no al revés; Psiquiatría ve Psicología, pero
    un psicólogo no ve Psiquiatría."""
    general = await add_doctor(db_session, specialty=GENERAL)
    psicologo = await add_doctor(db_session, specialty=PSICOLOGIA)
    interna = await _case(db_session, INTERNA)
    psiquiatria = await _case(db_session, PSIQUIATRIA)

    assert str(interna.id) not in await _waiting_ids(client, general.id)
    assert str(psiquiatria.id) not in await _waiting_ids(client, psicologo.id)


async def test_psiquiatria_tambien_ve_psicologia(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    psiquiatra = await add_doctor(db_session, specialty=PSIQUIATRIA)
    psicologia = await _case(db_session, PSICOLOGIA)
    general = await _case(db_session, GENERAL)

    ids = await _waiting_ids(client, psiquiatra.id)
    assert str(psicologia.id) in ids
    assert str(general.id) not in ids
    assert await _claim(client, psicologia.id, psiquiatra.id) == 200


async def test_medico_con_otra_no_ve_nada_y_el_panel_dice_por_que(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    otra = await add_doctor(db_session, specialty="Otra")
    caso = await _case(db_session, GENERAL)

    panel = await _panel(client, otra.id)
    assert panel["waiting"] == []
    assert panel["queue_blocked_reason"] == "especialidad_por_definir"
    assert await _claim(client, caso.id, otra.id) == 403


async def test_medico_sin_especialidad_no_ve_nada(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    sin = await add_doctor(db_session)
    await _case(db_session, GENERAL)

    panel = await _panel(client, sin.id)
    assert panel["waiting"] == []
    assert panel["queue_blocked_reason"] == "sin_especialidad"


async def test_super_admin_psicologo_solo_ve_psicologia(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El reporte de producción: Luis (psicólogo y super_admin) veía Medicina general."""
    luis = await add_doctor(db_session, role="super_admin", specialty=PSICOLOGIA)
    psicologia = await _case(db_session, PSICOLOGIA)
    general = await _case(db_session, GENERAL)

    ids = await _waiting_ids(client, luis.id)
    assert str(psicologia.id) in ids
    assert str(general.id) not in ids
    assert await _claim(client, general.id, luis.id) == 403


async def test_super_admin_medico_ve_todas_las_colas(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await add_doctor(db_session, role="super_admin", specialty=GENERAL)
    psicologia = await _case(db_session, PSICOLOGIA)
    trauma = await _case(db_session, TRAUMA)

    ids = await _waiting_ids(client, admin.id)
    assert {str(psicologia.id), str(trauma.id)} <= ids
    assert (await _panel(client, admin.id))["queue_blocked_reason"] is None


async def test_admin_sin_especialidad_o_con_otra_ve_todo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Las cuentas de administración pura no atienden, pero supervisan la cola completa."""
    sin = await _admin(db_session, "admin", None)
    con_otra = await _admin(db_session, "super_admin", "Otra")
    caso = await _case(db_session, TRAUMA)

    assert str(caso.id) in await _waiting_ids(client, sin.id)
    assert str(caso.id) in await _waiting_ids(client, con_otra.id)


async def test_la_cola_va_por_hora_de_llegada(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un caso derivado conserva su `queued_at`: aunque se creó después, va primero."""
    doc = await add_doctor(db_session, specialty=GENERAL)
    reciente = await _case(db_session, GENERAL)
    antiguo = await _case(db_session, GENERAL, queued_at=datetime.now(UTC) - timedelta(hours=5))

    waiting = (await _panel(client, doc.id))["waiting"]
    ids = [c["id"] for c in waiting]
    assert ids.index(str(antiguo.id)) < ids.index(str(reciente.id))
    item = next(c for c in waiting if c["id"] == str(antiguo.id))
    assert item["queued_at"] is not None
    assert item["derived_from_specialty"] is None


async def test_la_cola_solo_trae_casos_en_espera(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un caso sin médico pero ya no en espera (cerrado por un admin) no es de la cola."""
    doc = await add_doctor(db_session, specialty=GENERAL)
    cerrado = await _case(db_session, GENERAL)
    cerrado.status = "closed_by_admin"
    await db_session.flush()

    assert str(cerrado.id) not in await _waiting_ids(client, doc.id)


async def test_get_queue_y_take_legacy_aplican_la_misma_regla(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`GET /queue` y `/queue/{id}/take` no pueden ser un atajo alrededor del filtro."""
    trauma = await add_doctor(db_session, specialty=TRAUMA)
    suyo = await _case(db_session, TRAUMA)
    ajeno = await _case(db_session, GENERAL)
    headers = auth_headers(trauma.id)

    listed = await client.get(f"{PREFIX}/queue", headers=headers)
    assert listed.status_code == 200, listed.text
    ids = {c["id"] for c in listed.json()}
    assert str(suyo.id) in ids
    assert str(ajeno.id) not in ids

    assert (
        await client.post(f"{PREFIX}/queue/{ajeno.id}/take", headers=headers)
    ).status_code == 403
    took = await client.post(f"{PREFIX}/queue/{suyo.id}/take", headers=headers)
    assert took.status_code == 200, took.text
    assert "/vamed-" in took.json()["video_room_url"]


async def test_un_paciente_no_puede_pedir_otra(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "Otra" no es la cola de nadie: crear una consulta con ella es 422."""
    patient = Patient(
        full_name="Paciente Otra",
        phone_whatsapp="+584140000112",
        affected_zone="Caracas",
        consent=True,
    )
    db_session.add(patient)
    await db_session.flush()
    resp = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": str(patient.id),
            "specialty_id": str(await specialty_id_by_name(db_session, "Otra")),
        },
    )
    assert resp.status_code == 422, resp.text


async def test_el_catalogo_marca_la_especialidad_de_relleno(client: AsyncClient) -> None:
    specs = (await client.get(f"{PREFIX}/specialties")).json()
    otra = next(s for s in specs if s["name"].lower() == "otra")
    general = next(s for s in specs if s["name"].lower() == GENERAL.lower())
    assert otra["is_placeholder"] is True
    assert general["is_placeholder"] is False
