"""Gate de credencial médica: quien no tiene la credencial verificada NO atiende.

Regla de producto: un médico sin cédula y licencia verificadas (SACS/FPV) no puede hacer
nada en la plataforma hasta que un admin lo apruebe. El gate vive en
`doctors.has_valid_credential` y lo aplica `get_current_principal`, que le vacía los
permisos al principal (mismo efecto que una cuenta revocada) sin quitarle el rol.

Lo que NO debe bloquear: ver y completar su propia ficha (`/doctors/me`) — es la vía por
la que el médico sale del limbo — ni a los admin, que no dependen de tener ficha.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests._helpers import add_doctor, auth_headers, make_doctor_row, make_profile

PREFIX = "/api/v1"


async def _queue_status(client: AsyncClient, profile_id) -> int:
    """Código de la lectura de la cola (`queue.read`) para ese usuario."""
    resp = await client.get(f"{PREFIX}/queue", headers=auth_headers(profile_id))
    return resp.status_code


# --- Bloqueados: los tres sabores de "no debería estar atendiendo" ---


async def test_medico_sin_ficha_no_puede_operar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cuenta de Google que eligió rol `doctor` pero nunca registró su cédula."""
    doc = make_profile(role="doctor")
    db_session.add(doc)
    await db_session.flush()

    assert await _queue_status(client, doc.id) == 403


async def test_medico_con_ficha_rechazada_por_sacs_no_puede_operar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El registro creó la ficha pero el SACS no validó la cédula (fail-closed)."""
    doc = await add_doctor(db_session, verified=False)

    assert await _queue_status(client, doc.id) == 403


async def test_ficha_verificada_sin_cedula_ni_licencia_no_puede_operar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El caso legacy: filas backfilleadas con `verified=true` que nunca pasaron por el
    SACS y no tienen cédula ni licencia. Marcar verificado no basta: sin los datos que
    sustentan la credencial, no atiende."""
    sin_cedula = await add_doctor(db_session, cedula=None)
    sin_licencia = await add_doctor(db_session, license=None)

    assert await _queue_status(client, sin_cedula.id) == 403
    assert await _queue_status(client, sin_licencia.id) == 403


async def test_medico_de_baja_o_expulsado_no_puede_operar(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`status` 0 (se dio de baja) y 2 (expulsado) tampoco atienden, aunque la
    credencial esté verificada."""
    de_baja = await add_doctor(db_session, status=0)
    expulsado = await add_doctor(db_session, status=2)

    assert await _queue_status(client, de_baja.id) == 403
    assert await _queue_status(client, expulsado.id) == 403


async def test_el_gate_tambien_cierra_las_rutas_de_require_staff(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """No todas las puertas van por `require_permission`: las interconsultas exigen
    `require_staff`. El gate vive en el Principal, así que las cubre igual."""
    doc = await add_doctor(db_session, verified=False)

    resp = await client.get(f"{PREFIX}/interconsultations/me", headers=auth_headers(doc.id))
    assert resp.status_code == 403


async def test_el_403_del_gate_explica_que_falta_aprobacion(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El mensaje distingue "te falta la aprobación" de un "no tienes permiso" genérico,
    para que el frontend muestre el estado pendiente."""
    doc = await add_doctor(db_session, verified=False)

    resp = await client.get(f"{PREFIX}/queue", headers=auth_headers(doc.id))
    assert resp.status_code == 403
    assert "verificada" in resp.json()["detail"]


# --- Habilitados ---


async def test_medico_con_credencial_completa_opera(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session)

    assert await _queue_status(client, doc.id) == 200


async def test_admin_sin_ficha_no_se_ve_afectado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El gate es de médicos: un admin no tiene ficha en `doctors` y debe seguir entrando
    (si no, nadie podría aprobar a nadie)."""
    admin = make_profile(role="admin")
    db_session.add(admin)
    await db_session.flush()

    assert await _queue_status(client, admin.id) == 200


async def test_medico_que_ademas_es_admin_no_queda_bloqueado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Rol dual: si la cuenta es admin, su ficha de médico sin verificar no la bloquea."""
    dual = make_profile(role="admin")
    db_session.add(dual)
    await db_session.flush()
    db_session.add(make_doctor_row(dual.id, verified=False))
    await db_session.flush()

    assert await _queue_status(client, dual.id) == 200


# --- La salida del limbo ---


async def test_aprobacion_del_admin_habilita_al_medico(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El camino de aprobación manual: el SACS rechazó al médico, un admin marca la ficha
    como verificada (`PATCH /doctors/{id}`) y a partir de ahí puede atender."""
    doc = await add_doctor(db_session, verified=False)
    assert await _queue_status(client, doc.id) == 403

    ficha = (await client.get(f"{PREFIX}/doctors/me", headers=auth_headers(doc.id))).json()
    # `client` va autenticado como admin.
    aprobacion = await client.patch(
        f"{PREFIX}/doctors/{ficha['doctor_id']}", json={"verified": True}
    )
    assert aprobacion.status_code == 200, aprobacion.text

    assert await _queue_status(client, doc.id) == 200


async def test_medico_pendiente_puede_ver_y_editar_su_propia_ficha(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Sin esto el gate sería una trampa sin salida: el médico bloqueado tiene que poder
    entrar a corregir su cédula/licencia. `/doctors/me` no pasa por `require_permission`."""
    doc = await add_doctor(db_session, verified=False)

    ver = await client.get(f"{PREFIX}/doctors/me", headers=auth_headers(doc.id))
    assert ver.status_code == 200
    assert ver.json()["verified"] is False

    editar = await client.patch(
        f"{PREFIX}/doctors/me",
        headers=auth_headers(doc.id),
        json={"license": "MPPS-99999"},
    )
    assert editar.status_code == 200, editar.text
    assert editar.json()["license"] == "MPPS-99999"


async def test_permissions_expone_el_estado_pendiente(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`/auth/me/permissions` es lo que lee el frontend: el médico pendiente conserva su
    rol pero llega sin permisos y con `credential_verified: false`."""
    doc = await add_doctor(db_session, verified=False)

    resp = await client.get(f"{PREFIX}/auth/me/permissions", headers=auth_headers(doc.id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["roles"] == ["doctor"]
    assert body["permissions"] == []
    assert body["credential_verified"] is False


async def test_permissions_de_medico_habilitado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = await add_doctor(db_session)

    body = (await client.get(f"{PREFIX}/auth/me/permissions", headers=auth_headers(doc.id))).json()
    assert body["credential_verified"] is True
    assert "queue.take" in body["permissions"]
