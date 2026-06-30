"""Pruebas del middleware de Correlation-ID."""

from httpx import AsyncClient


async def test_correlation_id_is_added(live_client: AsyncClient) -> None:
    resp = await live_client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("X-Correlation-ID")


async def test_correlation_id_is_echoed(live_client: AsyncClient) -> None:
    resp = await live_client.get("/", headers={"X-Correlation-ID": "abc-123"})
    assert resp.headers.get("X-Correlation-ID") == "abc-123"
