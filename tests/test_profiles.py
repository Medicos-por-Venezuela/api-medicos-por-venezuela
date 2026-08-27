"""Pruebas del recurso profiles (solo lectura; usa datos ya restaurados)."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.doctor import Doctor
from src.models.profile import Profile
from tests._helpers import make_profile

PREFIX = "/api/v1"


async def test_list_and_get_profile(client: AsyncClient) -> None:
    listed = await client.get(f"{PREFIX}/profiles", params={"limit": 1})
    assert listed.status_code == 200
    body = listed.json()
    # Respuesta paginada: {items, total}. El total cuenta TODO (no solo la página de limit=1).
    assert len(body["items"]) == 1
    assert body["total"] >= 1
    profile_id = body["items"][0]["id"]

    got = await client.get(f"{PREFIX}/profiles/{profile_id}")
    assert got.status_code == 200
    assert got.json()["id"] == profile_id


async def test_list_profiles_filter_role(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/profiles", params={"role": "doctor", "limit": 5})
    assert resp.status_code == 200
    assert all(p["role"] == "doctor" for p in resp.json()["items"])


async def test_list_profiles_multi_role_active_and_total(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    activo = make_profile(role="specialist")
    revocado = make_profile(role="admin")
    revocado.active = False
    db_session.add_all([activo, revocado])
    await db_session.flush()

    # roles múltiples: specialist + admin.
    resp = await client.get(
        f"{PREFIX}/profiles", params=[("roles", "specialist"), ("roles", "admin"), ("limit", 100)]
    )
    assert resp.status_code == 200
    body = resp.json()
    got = {p["id"] for p in body["items"]}
    assert str(activo.id) in got and str(revocado.id) in got
    assert body["total"] >= len(body["items"])

    # active=false → solo revocados.
    only_revoked = await client.get(f"{PREFIX}/profiles", params={"active": "false", "limit": 100})
    ids = {p["id"] for p in only_revoked.json()["items"]}
    assert str(revocado.id) in ids and str(activo.id) not in ids


async def test_list_profiles_search_by_name(client: AsyncClient, db_session: AsyncSession) -> None:
    hit = make_profile(role="doctor")
    hit.full_name = "Zoraida Buscada Perez"
    miss = make_profile(role="doctor")
    miss.full_name = "Otro Distinto"
    db_session.add_all([hit, miss])
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/profiles", params={"search": "oraida", "limit": 100})
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()["items"]}
    assert str(hit.id) in ids
    assert str(miss.id) not in ids


async def test_list_profiles_search_by_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doc = make_profile(role="doctor")
    doc.email = "buscame.unico@example.com"
    db_session.add(doc)
    await db_session.flush()

    resp = await client.get(f"{PREFIX}/profiles", params={"search": "buscame.unico", "limit": 100})
    assert resp.status_code == 200
    assert str(doc.id) in {p["id"] for p in resp.json()["items"]}


async def test_profile_not_found(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/profiles/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def _named(role: str) -> Profile:
    """Perfil con nombre único: la BD local tiene miles de perfiles restaurados de prod, así que
    `search` necesita un término que solo case con el de esta prueba."""
    profile = make_profile(role=role)
    profile.full_name = f"ZZTest {uuid.uuid4()}"
    return profile


async def _fetch_one(client: AsyncClient, profile: Profile) -> dict:
    """La fila de ese perfil en GET /profiles, exigiendo que salga EXACTAMENTE una (si el join
    duplicara filas, esto es lo que lo caza)."""
    resp = await client.get(
        f"{PREFIX}/profiles", params={"search": profile.full_name, "limit": 100}
    )
    assert resp.status_code == 200
    matches = [p for p in resp.json()["items"] if p["id"] == str(profile.id)]
    assert len(matches) == 1, f"esperaba 1 fila para {profile.id}, hubo {len(matches)}"
    return matches[0]


async def test_doctor_verified_refleja_la_ficha_no_users_verified(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El listado debe exponer el `verified` de `doctors` (resultado real de SACS/FPV), no el de
    `users`, que nace true y ningún camino la baja. Antes el admin veía a TODO el mundo como
    verificado porque la lista leía la columna equivocada."""
    con_cedula_ok = _named("doctor")
    con_cedula_mal = _named("doctor")
    sin_ficha = _named("patient")
    db_session.add_all([con_cedula_ok, con_cedula_mal, sin_ficha])
    await db_session.flush()

    # Las tres cuentas tienen users.verified = True (make_profile lo fija así, igual que el
    # trigger en producción): si la respuesta distingue entre ellas, es que NO está leyendo esa
    # columna.
    assert con_cedula_ok.verified and con_cedula_mal.verified and sin_ficha.verified

    db_session.add_all(
        [
            Doctor(user_id=con_cedula_ok.id, full_name="Dr Cedula OK", verified=True),
            Doctor(user_id=con_cedula_mal.id, full_name="Dr Cedula Mal", verified=False),
        ]
    )
    await db_session.flush()

    assert (await _fetch_one(client, con_cedula_ok))["doctor_verified"] is True
    assert (await _fetch_one(client, con_cedula_mal))["doctor_verified"] is False
    # Sin ficha de médico no hay credencial que verificar: None, no False.
    assert (await _fetch_one(client, sin_ficha))["doctor_verified"] is None


async def test_ficha_borrada_no_duplica_la_fila_ni_falsea_el_estado(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El índice único de doctors.user_id es PARCIAL (solo WHERE deleted_at IS NULL), así que un
    médico puede acumular fichas borradas. El join las ignora: ni duplican su fila en la página
    (descuadrando el total) ni resucitan su `verified`."""
    perfil = _named("doctor")
    db_session.add(perfil)
    await db_session.flush()
    db_session.add_all(
        [
            Doctor(
                user_id=perfil.id,
                full_name="Ficha vieja",
                verified=True,
                deleted_at=datetime.now(UTC),
            ),
            Doctor(user_id=perfil.id, full_name="Ficha viva", verified=False),
        ]
    )
    await db_session.flush()

    # _fetch_one ya asegura que hay exactamente UNA fila para este perfil.
    assert (await _fetch_one(client, perfil))["doctor_verified"] is False


async def test_solo_fichas_borradas_devuelve_none(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un médico cuya única ficha está borrada cuenta como 'sin ficha', no como no verificado."""
    perfil = _named("doctor")
    db_session.add(perfil)
    await db_session.flush()
    db_session.add(
        Doctor(
            user_id=perfil.id,
            full_name="Solo borrada",
            verified=True,
            deleted_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    assert (await _fetch_one(client, perfil))["doctor_verified"] is None


async def test_la_lista_sigue_siendo_dos_consultas(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Anti-N+1: el `verified` de cada médico se trae en el JOIN de la página, no con un SELECT por
    fila. Con ~3500 usuarios, un N+1 aquí hunde la pantalla que más usa el admin. Son dos: el
    COUNT del total y el SELECT de la página."""
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db_session.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        resp = await client.get(f"{PREFIX}/profiles", params={"role": "doctor", "limit": 50})
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert resp.status_code == 200
    assert len(resp.json()["items"]) > 1, "el test no prueba nada con una sola fila"
    sobre_users = [s for s in statements if "users" in s.lower()]
    assert len(sobre_users) == 2, (
        f"esperaba COUNT + SELECT, hubo {len(sobre_users)}:\n" + "\n---\n".join(sobre_users)
    )
