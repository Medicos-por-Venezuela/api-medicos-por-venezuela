"""Aprobación manual de médicos y listado admin filtrable.

Contexto: el gate de credencial (`test_doctor_credential_gate.py`) deja fuera a casi todos
los médicos reales. Estos son los dos endpoints con los que un admin lo gestiona:

- `POST /doctors/{id}/approve` (permiso `doctors.verify`): habilita al médico que el
  registro oficial no validó, con su propia entrada en `audit_log`. Y **se niega** cuando
  aprobar no serviría de nada, en vez de devolver un 200 mentiroso.
- `GET /doctors`: la tabla del panel, con el motivo de bloqueo de cada uno para que el
  admin sepa a quién puede aprobar y a quién hay que pedirle la cédula.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.profile import Profile
from tests._helpers import add_doctor, auth_headers, make_profile

PREFIX = "/api/v1"


def _marker() -> str:
    """Nombre único: la BD local tiene ~3000 médicos reales, así que todo listado se
    consulta con `search=<marker>` para que el test no dependa de ellos."""
    return f"ZZMARK{uuid.uuid4().hex[:10]}"


async def _doctor_id(client: AsyncClient, profile: Profile) -> str:
    """id de la ficha del médico, leído por su propio `/doctors/me` (fuera del gate)."""
    resp = await client.get(f"{PREFIX}/doctors/me", headers=auth_headers(profile.id))
    assert resp.status_code == 200, resp.text
    return resp.json()["doctor_id"]


async def _queue_status(client: AsyncClient, profile: Profile) -> int:
    return (await client.get(f"{PREFIX}/queue", headers=auth_headers(profile.id))).status_code


async def _rows(client: AsyncClient, **params) -> list[dict]:
    resp = await client.get(f"{PREFIX}/doctors", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


# --- POST /doctors/{id}/approve ---


async def test_aprobar_habilita_al_medico_que_el_sacs_no_valido(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El camino feliz completo: bloqueado -> un admin aprueba -> atiende."""
    doc = await add_doctor(db_session, verified=False)
    assert await _queue_status(client, doc) == 403

    resp = await client.post(f"{PREFIX}/doctors/{await _doctor_id(client, doc)}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"] is True

    assert await _queue_status(client, doc) == 200


async def test_aprobar_deja_su_propia_traza_en_audit_log(
    client: AsyncClient, db_session: AsyncSession, admin_identity: Profile
) -> None:
    """`doctor.approved`, no un `doctor.updated` genérico: es el registro de que un humano
    —y cuál— dejó atender a alguien que el registro oficial no respaldó."""
    doc = await add_doctor(db_session, verified=False)
    doctor_id = await _doctor_id(client, doc)

    assert (await client.post(f"{PREFIX}/doctors/{doctor_id}/approve")).status_code == 200

    audit = await client.get(f"{PREFIX}/audit-log", params={"action": "doctor.approved"})
    entries = [e for e in audit.json() if e["resource_id"] == doctor_id]
    assert len(entries) == 1
    assert entries[0]["actor_user_id"] == str(admin_identity.id)
    assert entries[0]["resource"] == "doctors"


async def test_aprobar_sin_cedula_es_422_y_dice_que_falta(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El caso de la inmensa mayoría de los bloqueados: verificado legacy sin cédula.
    Aprobar no los habilitaría (el gate exige la cédula), así que se rechaza en vez de
    dejar al admin creyendo que ya está."""
    doc = await add_doctor(db_session, verified=False, cedula=None)

    resp = await client.post(f"{PREFIX}/doctors/{await _doctor_id(client, doc)}/approve")
    assert resp.status_code == 422, resp.text
    assert "cédula" in resp.json()["detail"]

    assert await _queue_status(client, doc) == 403


async def test_aprobar_sin_licencia_es_422(client: AsyncClient, db_session: AsyncSession) -> None:
    doc = await add_doctor(db_session, verified=False, license="   ")

    resp = await client.post(f"{PREFIX}/doctors/{await _doctor_id(client, doc)}/approve")
    assert resp.status_code == 422, resp.text
    assert "licencia" in resp.json()["detail"]


async def test_aprobar_ficha_de_baja_o_expulsada_es_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`status` 0/2 tampoco atiende: aprobar sin reactivar sería otro 200 inútil."""
    for status in (0, 2):
        doc = await add_doctor(db_session, verified=False, status=status)
        resp = await client.post(f"{PREFIX}/doctors/{await _doctor_id(client, doc)}/approve")
        assert resp.status_code == 422, f"status={status} -> {resp.text}"


async def test_aprobar_es_idempotente(client: AsyncClient, db_session: AsyncSession) -> None:
    doc = await add_doctor(db_session, verified=False)
    doctor_id = await _doctor_id(client, doc)

    assert (await client.post(f"{PREFIX}/doctors/{doctor_id}/approve")).status_code == 200
    repetida = await client.post(f"{PREFIX}/doctors/{doctor_id}/approve")
    assert repetida.status_code == 200
    assert repetida.json()["verified"] is True


async def test_aprobar_medico_inexistente_404(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.post(f"{PREFIX}/doctors/{missing}/approve")).status_code == 404


async def test_aprobar_exige_el_permiso_doctors_verify(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un médico habilitado (tiene doctors.read, no doctors.verify) no se autoaprueba a
    sí mismo ni aprueba a nadie."""
    otro = await add_doctor(db_session, verified=False)
    doctor_id = await _doctor_id(client, otro)
    medico = await add_doctor(db_session)

    resp = await client.post(
        f"{PREFIX}/doctors/{doctor_id}/approve", headers=auth_headers(medico.id)
    )
    assert resp.status_code == 403


async def test_patch_ya_no_puede_marcar_verified(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """La aprobación tiene endpoint propio para que su traza en `audit_log` no sea
    opcional: colarla por el PATCH genérico la volvería un `doctor.updated` cualquiera."""
    doc = await add_doctor(db_session, verified=False)
    doctor_id = await _doctor_id(client, doc)

    resp = await client.patch(f"{PREFIX}/doctors/{doctor_id}", json={"verified": True})
    assert resp.status_code == 422
    assert await _queue_status(client, doc) == 403


# --- POST /doctors/{id}/revoke-approval ---


async def test_revocar_aprobacion_devuelve_al_medico_al_limbo(
    client: AsyncClient, db_session: AsyncSession, admin_identity: Profile
) -> None:
    doc = await add_doctor(db_session)
    doctor_id = await _doctor_id(client, doc)
    assert await _queue_status(client, doc) == 200

    resp = await client.post(f"{PREFIX}/doctors/{doctor_id}/revoke-approval")
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"] is False

    assert await _queue_status(client, doc) == 403

    audit = await client.get(f"{PREFIX}/audit-log", params={"action": "doctor.approval_revoked"})
    entries = [e for e in audit.json() if e["resource_id"] == doctor_id]
    assert len(entries) == 1
    assert entries[0]["actor_user_id"] == str(admin_identity.id)


async def test_revocar_no_exige_ficha_completa(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Quitar el permiso siempre es válido, aunque la ficha esté incompleta."""
    doc = await add_doctor(db_session, license=None)

    resp = await client.post(f"{PREFIX}/doctors/{await _doctor_id(client, doc)}/revoke-approval")
    assert resp.status_code == 200, resp.text


async def test_revocar_medico_inexistente_404(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.post(f"{PREFIX}/doctors/{missing}/revoke-approval")).status_code == 404


# --- GET /doctors: motivos de bloqueo ---


async def test_listado_expone_el_motivo_de_bloqueo_de_cada_uno(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Los cinco sabores, cada uno con el motivo que le dice al admin qué hacer."""
    marker = _marker()
    habilitado = await add_doctor(db_session, full_name=f"{marker} ok")
    no_verificado = await add_doctor(db_session, full_name=f"{marker} nv", verified=False)
    sin_cedula = await add_doctor(db_session, full_name=f"{marker} sc", cedula=None)
    sin_licencia = await add_doctor(db_session, full_name=f"{marker} sl", license=None)
    de_baja = await add_doctor(db_session, full_name=f"{marker} db", status=0)
    sin_ficha = make_profile(role="doctor")
    sin_ficha.full_name = f"{marker} sf"
    db_session.add(sin_ficha)
    await db_session.flush()

    by_user = {row["user_id"]: row for row in await _rows(client, search=marker)}
    assert len(by_user) == 6

    assert by_user[str(habilitado.id)]["blocked_reason"] is None
    assert by_user[str(habilitado.id)]["can_practice"] is True
    assert by_user[str(no_verificado.id)]["blocked_reason"] == "no_verificado"
    assert by_user[str(sin_cedula.id)]["blocked_reason"] == "sin_cedula"
    assert by_user[str(sin_licencia.id)]["blocked_reason"] == "sin_licencia"
    assert by_user[str(de_baja.id)]["blocked_reason"] == "de_baja"

    huerfano = by_user[str(sin_ficha.id)]
    assert huerfano["blocked_reason"] == "sin_ficha"
    assert huerfano["id"] is None  # no hay ficha que aprobar
    assert huerfano["status"] is None


async def test_no_verificado_es_exactamente_lo_que_el_boton_de_aprobar_arregla(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El invariante que hace útil la tabla: `blocked_reason == "no_verificado"` <=>
    aprobar funciona. Con cualquier otro motivo, aprobar da 422."""
    marker = _marker()
    aprobable = await add_doctor(db_session, full_name=f"{marker} nv", verified=False)
    no_aprobable = await add_doctor(db_session, full_name=f"{marker} sc", cedula=None)

    by_user = {row["user_id"]: row for row in await _rows(client, search=marker)}
    assert by_user[str(aprobable.id)]["blocked_reason"] == "no_verificado"
    assert by_user[str(no_aprobable.id)]["blocked_reason"] == "sin_cedula"

    ok = await client.post(f"{PREFIX}/doctors/{by_user[str(aprobable.id)]['id']}/approve")
    assert ok.status_code == 200
    ko = await client.post(f"{PREFIX}/doctors/{by_user[str(no_aprobable.id)]['id']}/approve")
    assert ko.status_code == 422


# --- GET /doctors: filtros, búsqueda y paginación ---


async def test_filtro_verified_no_es_lo_mismo_que_habilitado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """La distinción que le importa al admin: hay fichas `verified=true` que NO atienden
    (les falta la cédula). Filtrar por una y por otra da resultados distintos."""
    marker = _marker()
    habilitado = await add_doctor(db_session, full_name=f"{marker} ok")
    verificado_sin_cedula = await add_doctor(db_session, full_name=f"{marker} sc", cedula=None)

    verificados = {r["user_id"] for r in await _rows(client, search=marker, verified=True)}
    assert verificados == {str(habilitado.id), str(verificado_sin_cedula.id)}

    pueden = {r["user_id"] for r in await _rows(client, search=marker, can_practice=True)}
    assert pueden == {str(habilitado.id)}

    bloqueados = {r["user_id"] for r in await _rows(client, search=marker, can_practice=False)}
    assert bloqueados == {str(verificado_sin_cedula.id)}


async def test_filtro_por_motivo_aisla_a_los_que_se_pueden_aprobar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El filtro operativo del panel: sin él los aprobables se pierden.

    En los datos reales el 95% de los bloqueados es `sin_cedula`, así que un listado
    ordenado por fecha no enseña ni un solo médico aprobable en la primera página y el
    admin concluye que el botón de aprobar no existe. Filtrar por motivo es lo que
    convierte la tabla en una cola de trabajo.
    """
    marker = _marker()
    aprobable = await add_doctor(db_session, full_name=f"{marker} nv", verified=False)
    await add_doctor(db_session, full_name=f"{marker} sc", cedula=None)
    await add_doctor(db_session, full_name=f"{marker} sl", license=None)

    solo_aprobables = await _rows(client, search=marker, blocked_reason="no_verificado")
    assert [r["user_id"] for r in solo_aprobables] == [str(aprobable.id)]
    # Y todos los que salen ahí tienen ficha, así que hay botón que pulsar.
    assert solo_aprobables[0]["id"] is not None

    sin_cedula = await _rows(client, search=marker, blocked_reason="sin_cedula")
    assert [r["blocked_reason"] for r in sin_cedula] == ["sin_cedula"]


async def test_filtro_por_motivo_rechaza_un_valor_inventado(client: AsyncClient) -> None:
    """El motivo es un enum cerrado: un valor libre sería un filtro que no filtra nada."""
    resp = await client.get(f"{PREFIX}/doctors", params={"blocked_reason": "cualquier_cosa"})
    assert resp.status_code == 422


async def test_filtro_verified_false(client: AsyncClient, db_session: AsyncSession) -> None:
    marker = _marker()
    await add_doctor(db_session, full_name=f"{marker} ok")
    pendiente = await add_doctor(db_session, full_name=f"{marker} nv", verified=False)
    sin_ficha = make_profile(role="doctor")
    sin_ficha.full_name = f"{marker} sf"
    db_session.add(sin_ficha)
    await db_session.flush()

    # Una cuenta sin ficha no tiene credencial aprobada: cuenta como no verificada.
    no_verificados = {r["user_id"] for r in await _rows(client, search=marker, verified=False)}
    assert no_verificados == {str(pendiente.id), str(sin_ficha.id)}


async def test_busqueda_por_nombre_cedula_y_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    marker = _marker()
    doc = await add_doctor(
        db_session,
        full_name=f"{marker} buscable",
        cedula="V-90007777",
        email=f"{marker.lower()}@test.com",
    )

    for term in (marker, "V-90007777", f"{marker.lower()}@test.com"):
        found = await _rows(client, search=term)
        assert [r["user_id"] for r in found] == [str(doc.id)], term


async def test_paginacion_disjunta_y_total_exacto(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El total es el de la consulta completa, no el de la página; y el desempate por id
    evita que una fila salga en dos páginas (todas comparten `created_at`)."""
    marker = _marker()
    for i in range(5):
        await add_doctor(db_session, full_name=f"{marker} {i}")

    primera = await client.get(f"{PREFIX}/doctors", params={"search": marker, "limit": 2})
    segunda = await client.get(
        f"{PREFIX}/doctors", params={"search": marker, "limit": 2, "skip": 2}
    )
    assert primera.json()["total"] == 5
    assert segunda.json()["total"] == 5

    ids_1 = [r["id"] for r in primera.json()["items"]]
    ids_2 = [r["id"] for r in segunda.json()["items"]]
    assert len(ids_1) == len(ids_2) == 2
    assert not set(ids_1) & set(ids_2)


# --- GET /doctors/credential-summary ---


async def test_resumen_cuadra_con_los_filtros_del_listado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Los contadores de la cabecera tienen que ser los mismos números que devuelve el
    listado al filtrar; si no, el admin hace clic en "26 por aprobar" y ve otra cosa.

    Se comprueba sobre el delta que introduce este test (la BD local trae ~3000 médicos
    reales), que es lo que se puede afirmar sin depender de esos datos."""
    antes = (await client.get(f"{PREFIX}/doctors/credential-summary")).json()

    await add_doctor(db_session, verified=False)  # no_verificado
    await add_doctor(db_session, cedula=None)  # sin_cedula
    await add_doctor(db_session)  # habilitado

    despues = (await client.get(f"{PREFIX}/doctors/credential-summary")).json()
    assert despues["no_verificado"] == antes["no_verificado"] + 1
    assert despues["sin_cedula"] == antes["sin_cedula"] + 1
    assert despues["can_practice"] == antes["can_practice"] + 1
    assert despues["total"] == antes["total"] + 3

    # Y el contador coincide con el total del listado filtrado por ese motivo.
    listado = await client.get(f"{PREFIX}/doctors", params={"blocked_reason": "no_verificado"})
    assert listado.json()["total"] == despues["no_verificado"]


async def test_resumen_suma_todos_los_estados(client: AsyncClient) -> None:
    """`total` es la suma de los grupos: ningún médico se queda fuera de la foto."""
    body = (await client.get(f"{PREFIX}/doctors/credential-summary")).json()
    grupos = (
        "can_practice",
        "sin_ficha",
        "de_baja",
        "sin_cedula",
        "sin_licencia",
        "no_verificado",
    )
    assert body["total"] == sum(body[g] for g in grupos)
    assert body["total"] == (await client.get(f"{PREFIX}/doctors")).json()["total"]


async def test_resumen_exige_doctors_read(client: AsyncClient, db_session: AsyncSession) -> None:
    paciente = make_profile(role="patient")
    db_session.add(paciente)
    await db_session.flush()

    resp = await client.get(
        f"{PREFIX}/doctors/credential-summary", headers=auth_headers(paciente.id)
    )
    assert resp.status_code == 403


async def test_listado_exige_doctors_read(client: AsyncClient, db_session: AsyncSession) -> None:
    paciente = make_profile(role="patient")
    db_session.add(paciente)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/doctors", headers=auth_headers(paciente.id))
    assert resp.status_code == 403


async def test_can_practice_del_listado_coincide_con_el_gate(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """La tabla y el backend tienen que decir lo mismo: si el listado dice que puede
    atender, la cola le responde 200; si dice que no, 403."""
    marker = _marker()
    casos = [
        await add_doctor(db_session, full_name=f"{marker} ok"),
        await add_doctor(db_session, full_name=f"{marker} nv", verified=False),
        await add_doctor(db_session, full_name=f"{marker} sc", cedula=None),
        await add_doctor(db_session, full_name=f"{marker} sl", license=None),
        await add_doctor(db_session, full_name=f"{marker} db", status=2),
    ]
    by_user = {row["user_id"]: row for row in await _rows(client, search=marker)}

    for doc in casos:
        esperado = 200 if by_user[str(doc.id)]["can_practice"] else 403
        assert await _queue_status(client, doc) == esperado, by_user[str(doc.id)]
