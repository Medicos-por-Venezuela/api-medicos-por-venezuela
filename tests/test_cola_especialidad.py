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
from tests._helpers import (
    GENERAL,
    add_doctor,
    auth_headers,
    make_profile,
    set_specialties,
    specialty_id_by_name,
)

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


async def test_traumatologo_ve_su_cola_y_la_de_entrada_pero_no_otras(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un especialista de salud física atiende lo suyo y la cola de entrada (Medicina general,
    donde caen los pacientes que no saben qué necesitan), pero no las colas de otros."""
    trauma = await add_doctor(db_session, specialty=TRAUMA)
    suyo = await _case(db_session, TRAUMA)
    entrada = await _case(db_session, GENERAL)
    ajeno = await _case(db_session, PEDIATRIA)

    ids = await _waiting_ids(client, trauma.id)
    assert {str(suyo.id), str(entrada.id)} <= ids
    assert str(ajeno.id) not in ids
    assert await _claim(client, ajeno.id, trauma.id) == 403
    assert await _claim(client, entrada.id, trauma.id) == 200
    assert await _claim(client, suyo.id, trauma.id) == 200


async def test_el_panel_separa_la_cola_propia_de_la_de_entrada(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El panel del especialista pinta dos cards: la de entrada (primero, la que más acumula) y la
    suya."""
    trauma = await add_doctor(db_session, specialty=TRAUMA)
    general = await add_doctor(db_session, specialty=GENERAL)
    psicologo = await add_doctor(db_session, specialty=PSICOLOGIA)

    colas = (await _panel(client, trauma.id))["queues"]
    assert [q["name"] for q in colas] == [GENERAL, TRAUMA]
    assert [q["is_triage"] for q in colas] == [True, False]

    # Para un médico general, la cola de entrada ES la suya: una sola card.
    colas_general = (await _panel(client, general.id))["queues"]
    assert [q["name"] for q in colas_general] == [GENERAL]
    assert colas_general[0]["is_triage"] is True

    # Psicología solo atiende salud mental: no se le ofrece la cola de entrada.
    assert [q["name"] for q in (await _panel(client, psicologo.id))["queues"]] == [PSICOLOGIA]


async def test_un_admin_que_no_es_especialista_no_ve_el_panel_partido_en_dos(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Quien ve TODAS las colas y solo ejerce Medicina general no tiene "la suya" y "la de
    entrada": son la misma, y partirlo en dos dejaba una card "mi especialidad" que en realidad
    traía todas las demás (reportado con una super_admin de Medicina general)."""
    con_general = await add_doctor(db_session, role="super_admin", specialty=GENERAL)
    sin_especialidad = await _admin(db_session, "admin", None)

    assert (await _panel(client, con_general.id))["queues"] == []
    assert (await _panel(client, sin_especialidad.id))["queues"] == []


async def test_un_admin_que_ademas_ejerce_ve_sus_colas_y_una_con_el_resto(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Una super_admin que también es cardióloga sí quiere sus colas separadas; pero sigue viendo
    TODO, así que la última card reúne lo que no es suyo — si no, los contadores de las cards no
    sumarían lo que dice el KPI y habría casos que no salen por ninguna parte."""
    admin = await add_doctor(db_session, role="super_admin", specialty=GENERAL)
    await set_specialties(db_session, admin.id, [GENERAL, "Cardiología"])
    entrada = await _case(db_session, GENERAL)
    cardio = await _case(db_session, "Cardiología")
    ajeno = await _case(db_session, TRAUMA)

    panel = await _panel(client, admin.id)

    assert [q["name"] for q in panel["queues"]] == [GENERAL, "Cardiología", "Otras especialidades"]
    assert [q["is_rest"] for q in panel["queues"]] == [False, False, True]
    # La del resto va sin ids: el panel la arma por descarte, para que una especialidad nueva no
    # se caiga de las cards.
    assert panel["queues"][-1]["id"] is None
    assert panel["queues"][-1]["specialty_ids"] == []
    # Sigue viendo todo, incluido lo que no es de sus especialidades.
    ids = {c["id"] for c in panel["waiting"]}
    assert {str(entrada.id), str(cardio.id), str(ajeno.id)} <= ids
    assert await _claim(client, ajeno.id, admin.id) == 200


async def test_un_medico_con_varias_especialidades_ve_las_colas_de_todas(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un internista que además es cardiólogo ve las dos colas, más la de entrada."""
    doc = await add_doctor(db_session, specialty=INTERNA)
    await set_specialties(db_session, doc.id, [INTERNA, "Cardiología"])
    interna = await _case(db_session, INTERNA)
    cardio = await _case(db_session, "Cardiología")
    entrada = await _case(db_session, GENERAL)
    ajeno = await _case(db_session, TRAUMA)

    panel = await _panel(client, doc.id)
    ids = {c["id"] for c in panel["waiting"]}
    assert {str(interna.id), str(cardio.id), str(entrada.id)} <= ids
    assert str(ajeno.id) not in ids
    assert [q["name"] for q in panel["queues"]] == [GENERAL, INTERNA, "Cardiología"]
    # La cola de Medicina general va en su propia card, no dentro de la de Medicina interna.
    interna_card = panel["queues"][1]
    assert str(entrada.specialty_id) not in interna_card["specialty_ids"]
    assert await _claim(client, cardio.id, doc.id) == 200


async def test_psicologia_no_ve_la_cola_de_entrada(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    psicologo = await add_doctor(db_session, specialty=PSICOLOGIA)
    entrada = await _case(db_session, GENERAL)

    assert str(entrada.id) not in await _waiting_ids(client, psicologo.id)
    assert await _claim(client, entrada.id, psicologo.id) == 403


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
    # (La cola de entrada la ven todos los de salud física; estas dos no son esa cola.)

    assert str(interna.id) not in await _waiting_ids(client, general.id)
    assert str(psiquiatria.id) not in await _waiting_ids(client, psicologo.id)


async def test_psiquiatria_tambien_ve_psicologia(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Psiquiatría atiende salud mental y física: ve Psicología, la suya y la de entrada, pero no
    la cola de otra especialidad física."""
    psiquiatra = await add_doctor(db_session, specialty=PSIQUIATRIA)
    psicologia = await _case(db_session, PSICOLOGIA)
    general = await _case(db_session, GENERAL)
    trauma = await _case(db_session, TRAUMA)

    ids = await _waiting_ids(client, psiquiatra.id)
    assert {str(psicologia.id), str(general.id)} <= ids
    assert str(trauma.id) not in ids
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
    ajeno = await _case(db_session, PEDIATRIA)
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
