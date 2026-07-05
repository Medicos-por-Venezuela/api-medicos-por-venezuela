"""decode_token soporta dos esquemas de firma: HS256 (secreto compartido, prod hoy) y
JWKS/ES256 (claves asimétricas rotables — el CLI de Supabase local firma así por defecto)."""

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from src.core import security
from src.core.config import settings

_KID = "test-kid"


class _FakeSigningKey:
    def __init__(self, key: object) -> None:
        self.key = key


class _FakeJWKClient:
    def __init__(self, public_key: object) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> _FakeSigningKey:
        return _FakeSigningKey(self._public_key)


@pytest.fixture
def es256_token_and_client():
    private_key = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode(
        {"sub": "11111111-1111-1111-1111-111111111111", "aud": "authenticated"},
        private_key,
        algorithm="ES256",
        headers={"kid": _KID},
    )
    return token, _FakeJWKClient(private_key.public_key())


def test_decode_token_valida_es256_via_jwks(monkeypatch, es256_token_and_client) -> None:
    token, fake_client = es256_token_and_client
    monkeypatch.setattr(settings, "SUPABASE_JWKS_URL", "http://fake/.well-known/jwks.json")
    monkeypatch.setattr(security, "_jwks_client", lambda _url: fake_client)

    payload = security.decode_token(token)
    assert payload["sub"] == "11111111-1111-1111-1111-111111111111"


def test_decode_token_es256_sin_jwks_url_falla(monkeypatch, es256_token_and_client) -> None:
    """Sin SUPABASE_JWKS_URL, un token ES256 cae al path HS256 y falla (esperado)."""
    token, _ = es256_token_and_client
    monkeypatch.setattr(settings, "SUPABASE_JWKS_URL", None)

    with pytest.raises(Exception):  # noqa: B017 — HTTPException 401 (_unauthorized)
        security.decode_token(token)


def test_decode_token_hs256_sigue_funcionando(monkeypatch) -> None:
    """Regresión: el esquema legacy (secreto compartido) no se rompe con el soporte JWKS."""
    monkeypatch.setattr(settings, "SUPABASE_JWKS_URL", None)
    token = jwt.encode(
        {"sub": "22222222-2222-2222-2222-222222222222", "aud": settings.SUPABASE_JWT_AUDIENCE},
        settings.SUPABASE_JWT_SECRET,
        algorithm=settings.SUPABASE_JWT_ALGORITHM,
    )
    payload = security.decode_token(token)
    assert payload["sub"] == "22222222-2222-2222-2222-222222222222"
