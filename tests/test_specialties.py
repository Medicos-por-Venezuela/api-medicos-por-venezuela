"""Pruebas unitarias del matching de especialidades (portado de lib/utils.ts)."""

from httpx import AsyncClient

from src.services.specialties import can_attend, compute_priority, matches_specialty

PREFIX = "/api/v1"


# --- matches_specialty ---


def test_matches_general_is_wildcard() -> None:
    assert matches_specialty("Medicina general", None, ["Lesión física"]) is True


def test_matches_by_need() -> None:
    assert matches_specialty("Psicología", None, ["Apoyo emocional"]) is True
    assert matches_specialty("Traumatología", None, ["Lesión física"]) is True


def test_matches_false_when_unrelated() -> None:
    assert matches_specialty("Cardiología", None, ["Lesión física"]) is False
    assert matches_specialty(None, None, ["x"]) is False


# --- can_attend (separación dura) ---


def test_reserved_need_blocks_general_doctor() -> None:
    # Un médico general NUNCA ve casos de salud mental.
    assert can_attend("Medicina general", None, ["Crisis de ansiedad"]) is False
    assert can_attend("Psiquiatría", None, ["Crisis de ansiedad"]) is True


def test_psychology_only_takes_psych_cases() -> None:
    assert can_attend("Psicología", None, ["Lesión física"]) is False
    assert can_attend("Psicología", None, ["Apoyo emocional"]) is True


def test_can_attend_physical_general() -> None:
    assert can_attend("Medicina general", None, ["Lesión física"]) is True


# --- compute_priority ---


def test_priority_review_for_sensitive_tags() -> None:
    assert compute_priority(["Embarazo"]) == "review"
    assert compute_priority(["Niño / pediatría"]) == "review"
    assert compute_priority(["Lesión física"]) == "review"
    assert compute_priority(["Medicina general"]) == "normal"
    assert compute_priority(None) == "normal"


# --- endpoint catálogo ---


async def test_specialties_catalog_endpoint(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/specialties")
    assert resp.status_code == 200
    body = resp.json()
    assert "Medicina general" in body["specialties"]
    assert "Apoyo emocional" in body["reserved_needs"]
    assert body["specialty_needs"]["Medicina general"] == ["*"]
