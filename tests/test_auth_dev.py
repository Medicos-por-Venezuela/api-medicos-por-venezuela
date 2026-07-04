"""Registro de DEV (/auth/dev/register): crea usuario+doctor y emite JWT, sin Supabase Auth."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.doctor import Doctor

PREFIX = "/api/v1"


async def test_dev_register_crea_cuenta_y_emite_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    resp = await client.post(
        f"{PREFIX}/auth/dev/register",
        json={
            "email": "dev.doctor@example.com",
            "full_name": "Dev Doctor",
            "role": "doctor",
            "specialty": "Cardiología",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["role"] == "doctor"

    # el token emitido funciona en /auth/me (sin pasar por Supabase)
    me = await client.get(
        f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200

    # dev/register crea SOLO la cuenta; el doctor lo crea el frontend vía POST /doctors.
    doc = (
        await db_session.execute(
            select(Doctor).where(Doctor.user_id == uuid.UUID(body["user_id"]))
        )
    ).scalar_one_or_none()
    assert doc is None


async def test_dev_register_idempotente_por_email(client: AsyncClient) -> None:
    payload = {"email": "dup@example.com", "full_name": "Dup User", "role": "doctor"}
    r1 = await client.post(f"{PREFIX}/auth/dev/register", json=payload)
    r2 = await client.post(f"{PREFIX}/auth/dev/register", json=payload)
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False
    assert r2.json()["user_id"] == r1.json()["user_id"]


async def test_dev_register_rechaza_admin(client: AsyncClient) -> None:
    resp = await client.post(
        f"{PREFIX}/auth/dev/register",
        json={"email": "a@example.com", "full_name": "Admin Nope", "role": "admin"},
    )
    assert resp.status_code == 422


async def test_dev_register_404_en_produccion(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    resp = await client.post(
        f"{PREFIX}/auth/dev/register",
        json={"email": "prod@example.com", "full_name": "No Prod", "role": "doctor"},
    )
    assert resp.status_code == 404


async def test_dev_login_devuelve_token_de_cuenta_existente(client: AsyncClient) -> None:
    reg = await client.post(
        f"{PREFIX}/auth/dev/register",
        json={"email": "login@example.com", "full_name": "Login User", "role": "doctor"},
    )
    resp = await client.post(f"{PREFIX}/auth/dev/login", json={"email": "login@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == reg.json()["user_id"]


async def test_dev_login_404_si_no_existe(client: AsyncClient) -> None:
    resp = await client.post(f"{PREFIX}/auth/dev/login", json={"email": "nadie@example.com"})
    assert resp.status_code == 404


async def test_dev_login_404_en_produccion(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    resp = await client.post(f"{PREFIX}/auth/dev/login", json={"email": "x@example.com"})
    assert resp.status_code == 404
