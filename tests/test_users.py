"""Pruebas de `POST /users` (creación administrativa de usuarios de Auth).

La Admin API de Supabase se mockea con `unittest.mock` (mismo patrón que
`test_sacs.py`): sin red real, sin dependencia del CLI de Supabase en CI. El
trigger `handle_new_auth_user()` tampoco corre contra un mock, así que estos
tests **seedan** la fila `Profile` en la sesión de savepoint para simular lo
que el trigger produciría en un entorno real (ver design.md "Testing Strategy").
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit_log import AuditLog
from src.models.profile import Profile
from src.models.rbac import Role, UserRole
from tests._helpers import auth_headers, make_profile

PREFIX = "/api/v1"

_PASSWORD = "S3guro!2026"


def _payload(email: str = "nuevo@example.com", initial_role: str | None = None) -> dict:
    body = {"email": email, "password": _PASSWORD, "full_name": "Nuevo Usuario"}
    if initial_role is not None:
        body["initial_role"] = initial_role
    return body


def _mock_admin_client(
    *,
    existing: dict | None = None,
    created: dict | None = None,
    get_side_effect: Exception | None = None,
    post_side_effect: Exception | None = None,
    get_json_error: Exception | None = None,
    post_json_error: Exception | None = None,
):
    """Parchea `httpx.AsyncClient` en `src.services.users` para simular la Admin API.

    `get_side_effect`/`post_side_effect` simulan la llamada de red en sí fallando
    (status de error o error de conexión). `get_json_error`/`post_json_error`
    simulan una respuesta 2xx con un body corrupto/inesperado (`.json()` falla).
    """
    get_resp = MagicMock()
    get_resp.raise_for_status = MagicMock()
    if get_json_error is not None:
        get_resp.json = MagicMock(side_effect=get_json_error)
    else:
        get_resp.json = MagicMock(return_value={"users": [existing] if existing else []})

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    if post_json_error is not None:
        post_resp.json = MagicMock(side_effect=post_json_error)
    else:
        post_resp.json = MagicMock(return_value=created or {})

    mock_client = AsyncMock()
    mock_client.get = (
        AsyncMock(side_effect=get_side_effect)
        if get_side_effect
        else AsyncMock(return_value=get_resp)
    )
    mock_client.post = (
        AsyncMock(side_effect=post_side_effect)
        if post_side_effect
        else AsyncMock(return_value=post_resp)
    )

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    return patch("src.services.users.httpx.AsyncClient", return_value=ctx), mock_client


async def _seed_profile(db_session: AsyncSession, user_id: uuid.UUID, email: str) -> Profile:
    """Simula la fila que `handle_new_auth_user()` crearía en un entorno real."""
    profile = Profile(
        id=user_id,
        email=email,
        full_name="Nuevo Usuario",
        role="patient",
        active=True,
        verified=True,
        role_chosen=False,
    )
    db_session.add(profile)
    await db_session.flush()
    return profile


async def test_crear_usuario_sin_rol_201(client: AsyncClient, db_session: AsyncSession) -> None:
    new_id = uuid.uuid4()
    await _seed_profile(db_session, new_id, "nuevo@example.com")

    patcher, _ = _mock_admin_client(created={"id": str(new_id), "email": "nuevo@example.com"})
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload())

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == str(new_id)
    assert body["email"] == "nuevo@example.com"
    assert body["role"] == "patient"
    assert "password" not in body

    created_rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "user.created", AuditLog.resource_id == str(new_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(created_rows) == 1

    role_rows = (
        (
            await db_session.execute(
                select(UserRole)
                .join(Role, Role.id == UserRole.role_id)
                .where(UserRole.user_id == new_id, Role.code != "patient")
            )
        )
        .scalars()
        .all()
    )
    assert role_rows == []


async def test_crear_usuario_con_rol_inicial_201(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    new_id = uuid.uuid4()
    await _seed_profile(db_session, new_id, "doctor.nuevo@example.com")

    patcher, _ = _mock_admin_client(
        created={"id": str(new_id), "email": "doctor.nuevo@example.com"}
    )
    with patcher:
        resp = await client.post(
            f"{PREFIX}/users",
            json=_payload(email="doctor.nuevo@example.com", initial_role="doctor"),
        )

    assert resp.status_code == 201, resp.text
    # `profiles.role` sigue siendo 'patient' (el trigger nunca ve `initial_role`,
    # solo llega vía `user_metadata` para patient/doctor); la respuesta debe
    # reflejar el rol RBAC realmente otorgado, no el legado del trigger.
    assert resp.json()["role"] == "doctor"

    actions = {
        row.action
        for row in (
            await db_session.execute(select(AuditLog).where(AuditLog.resource_id == str(new_id)))
        )
        .scalars()
        .all()
    }
    assert actions == {"user.created", "role.assigned"}


async def test_crear_usuario_email_duplicado_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    patcher, mock_client = _mock_admin_client(existing={"id": str(uuid.uuid4())})
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email="ya-existe@example.com"))

    assert resp.status_code == 409
    mock_client.post.assert_not_called()

    rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == "user.created")))
        .scalars()
        .all()
    )
    assert rows == []


async def test_crear_usuario_initial_role_super_admin_422(client: AsyncClient) -> None:
    patcher, mock_client = _mock_admin_client()
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(initial_role="super_admin"))

    assert resp.status_code == 422
    mock_client.get.assert_not_called()
    mock_client.post.assert_not_called()


async def test_crear_usuario_initial_role_super_admin_422_incluso_para_super_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Ni siquiera un actor super_admin puede bootstrapear super_admin vía POST /users."""
    super_admin = Profile(
        id=uuid.uuid4(),
        full_name="Test Super Admin",
        role="super_admin",
        active=True,
        verified=True,
        role_chosen=True,
    )
    db_session.add(super_admin)
    await db_session.flush()

    patcher, mock_client = _mock_admin_client()
    with patcher:
        resp = await client.post(
            f"{PREFIX}/users",
            json=_payload(initial_role="super_admin"),
            headers=auth_headers(super_admin.id),
        )

    assert resp.status_code == 422
    mock_client.post.assert_not_called()


async def test_crear_usuario_admin_api_falla_502(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from src.core.config import settings

    http_err = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock(status_code=500)
    )
    patcher, _ = _mock_admin_client(post_side_effect=http_err)
    with patcher, patch("src.services.users.logger") as mock_logger:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email="falla@example.com"))

    assert resp.status_code == 502

    assert mock_logger.error.called
    logged_text = " ".join(str(call_args) for call_args in mock_logger.error.call_args_list)
    assert settings.SUPABASE_SERVICE_ROLE_KEY not in logged_text


async def test_crear_usuario_rol_falla_no_revierte_usuario(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Auth creado; `initial_role` falla (ya estaba asignado, 409 de `assign_role`) ->
    el usuario NO se revierte (el `user.created` audit persiste) y un reintento con
    el mismo email es idempotente (409, sin una segunda llamada de creación)."""
    new_id = uuid.uuid4()
    await _seed_profile(db_session, new_id, "rolfalla@example.com")

    # Pre-existente: simula que el rol ya estaba asignado, forzando el ConflictError
    # que `assign_role` lanzaría en un fallo real entre la creación de Auth y el rol.
    doctor_role = (
        await db_session.execute(select(Role).where(Role.code == "doctor"))
    ).scalar_one()
    db_session.add(UserRole(user_id=new_id, role_id=doctor_role.id, assigned_by=new_id))
    await db_session.flush()

    patcher, _ = _mock_admin_client(created={"id": str(new_id), "email": "rolfalla@example.com"})
    with patcher:
        resp = await client.post(
            f"{PREFIX}/users",
            json=_payload(email="rolfalla@example.com", initial_role="doctor"),
        )

    assert resp.status_code == 409

    created_rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "user.created", AuditLog.resource_id == str(new_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(created_rows) == 1  # el hecho "usuario creado" quedó durable

    retry_patcher, retry_mock = _mock_admin_client(existing={"id": str(new_id)})
    with retry_patcher:
        retry = await client.post(f"{PREFIX}/users", json=_payload(email="rolfalla@example.com"))

    assert retry.status_code == 409
    retry_mock.post.assert_not_called()


async def test_crear_usuario_sin_permiso_403(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    doctor = make_profile(role="doctor")  # doctor no tiene 'users.create'
    db_session.add(doctor)
    await db_session.flush()

    patcher, mock_client = _mock_admin_client()
    with patcher:
        resp = await client.post(
            f"{PREFIX}/users",
            json=_payload(email="sin-permiso@example.com"),
            headers=auth_headers(doctor.id),
        )

    assert resp.status_code == 403
    mock_client.get.assert_not_called()
    mock_client.post.assert_not_called()


async def test_crear_usuario_payload_invalido_422(client: AsyncClient) -> None:
    resp = await client.post(
        f"{PREFIX}/users",
        json={
            "email": "malo@example.com",
            "password": "short",
            "full_name": "X",
            "campo_extra": "no permitido",
        },
    )
    assert resp.status_code == 422


async def test_crear_usuario_email_duplicado_race_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """El lookup idempotente no ve el email (aún no existe), pero dos requests
    concurrentes ganan la carrera contra la Admin API: `create` la rechaza (422,
    "ya registrado"). Debe reportarse 409 (conflicto real), NO 502 (no es una
    caída del proveedor)."""
    duplicate_err = httpx.HTTPStatusError(
        "422", request=MagicMock(), response=MagicMock(status_code=422)
    )
    patcher, mock_client = _mock_admin_client(post_side_effect=duplicate_err)
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email="carrera@example.com"))

    assert resp.status_code == 409, resp.text
    mock_client.get.assert_called_once()

    rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == "user.created")))
        .scalars()
        .all()
    )
    assert rows == []


async def test_crear_usuario_admin_api_respuesta_sin_id_502(client: AsyncClient) -> None:
    """La Admin API responde 2xx con JSON válido pero sin `id`: no debe escapar
    como un 500 sin manejar (KeyError), sino traducirse al 502 documentado."""
    patcher, _ = _mock_admin_client(created={"email": "sin-id@example.com"})
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email="sin-id@example.com"))

    assert resp.status_code == 502, resp.text


async def test_crear_usuario_lookup_falla_502(client: AsyncClient) -> None:
    """El lookup por email (GET) recibe un error HTTP del proveedor -> 502, no un
    500 sin manejar. (Antes solo se probaba esta rama para el POST de creación.)"""
    http_err = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock(status_code=500)
    )
    patcher, mock_client = _mock_admin_client(get_side_effect=http_err)
    email = "lookup-falla@example.com"
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email=email))

    assert resp.status_code == 502, resp.text
    mock_client.post.assert_not_called()


async def test_crear_usuario_lookup_conexion_falla_502(client: AsyncClient) -> None:
    """Error de conexión (no de status) en el lookup -> 502."""
    patcher, mock_client = _mock_admin_client(get_side_effect=httpx.ConnectError("boom"))
    email = "lookup-conexion@example.com"
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email=email))

    assert resp.status_code == 502, resp.text
    mock_client.post.assert_not_called()


async def test_crear_usuario_lookup_respuesta_malformada_502(client: AsyncClient) -> None:
    """El lookup responde 2xx pero `.json()` falla (body corrupto/no-JSON) -> 502,
    nunca un 500 sin manejar."""
    patcher, mock_client = _mock_admin_client(get_json_error=ValueError("invalid json"))
    email = "lookup-malformado@example.com"
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email=email))

    assert resp.status_code == 502, resp.text
    mock_client.post.assert_not_called()


async def test_crear_usuario_create_conexion_falla_502(client: AsyncClient) -> None:
    """Error de conexión (no de status) en la creación -> 502."""
    patcher, _ = _mock_admin_client(post_side_effect=httpx.ConnectError("boom"))
    email = "create-conexion@example.com"
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email=email))

    assert resp.status_code == 502, resp.text


async def test_crear_usuario_create_respuesta_malformada_502(client: AsyncClient) -> None:
    """La creación responde 2xx pero `.json()` falla (body corrupto/no-JSON) -> 502."""
    patcher, _ = _mock_admin_client(post_json_error=ValueError("invalid json"))
    email = "create-malformado@example.com"
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email=email))

    assert resp.status_code == 502, resp.text


async def test_crear_usuario_perfil_no_disponible_502(client: AsyncClient) -> None:
    """La Admin API confirma la creación pero el trigger todavía no produjo la
    fila `Profile` (no debería pasar en producción, pero si pasa es 502, no 500)."""
    new_id = uuid.uuid4()  # deliberadamente NO seedeado: no existe fila Profile
    patcher, _ = _mock_admin_client(created={"id": str(new_id), "email": "sin-perfil@example.com"})
    with patcher:
        resp = await client.post(f"{PREFIX}/users", json=_payload(email="sin-perfil@example.com"))

    assert resp.status_code == 502, resp.text
