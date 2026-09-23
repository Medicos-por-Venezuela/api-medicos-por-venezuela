"""Cifrado a nivel de campo de los datos clínicos (AES-256-GCM, clave SOLO en la API).

Qué protege: la LECTURA de la base sin la clave. Backups, el WAL de Realtime, el SQL Editor de
Supabase, un DBA o una policy RLS mal puesta ven `enc:v1:...`, no el motivo de consulta. La clave
vive únicamente en el entorno de la API (`CLINICAL_DATA_ENCRYPTION_KEY`); nunca en la base, nunca
en el cliente.

Qué NO protege:
- A la propia API: quien controla su proceso controla la clave. La frontera entre roles (el admin
  no ve lo clínico) la pone `src/schemas/clinical.py`, que solo descifra si el router concede un
  permiso explícito para ese objeto.
- A quien puede ESCRIBIR en la base. El AAD ata cada texto a su columna, no a su fila: copiar el
  motivo cifrado de un caso a otro propio lo haría legible por la API. No se ata a la fila porque
  no cerraría el hueco: con escritura basta con cambiar `patients.user_id` o `assigned_doctor_id`
  para que la API entregue el dato por la vía legítima. La escritura en la base se protege con
  credenciales y RLS, no con este cifrado.

Formato: `enc:v1:<kid>:<base64url(nonce_12 || ciphertext || tag_16)>`
- `kid` = 8 hex del SHA-256 de la clave. Permite rotar: se cifra con la activa y se descifra
  con cualquiera del llavero (`CLINICAL_DATA_ENCRYPTION_PREVIOUS_KEYS`).
- AAD = nombre de la columna (`consultations.internal_note`). Un texto cifrado movido a otra
  columna NO descifra: la nota interna del médico copiada al `chief_complaint` (que sí ve el
  paciente) falla en vez de mostrarse.
- Prefijo propio (`enc:`) distinto del `v1:` de la dirección cifrada E2E (`patients.
  address_encrypted`), que es otro esquema (sealed box, la API no tiene su clave privada).

Valores legados en claro (antes del backfill) se aceptan en lectura como `Sealed` sin cifrar:
siguen enmascarados para quien no tiene permiso, y `scripts/encrypt_clinical_data.py` los
cifra. Tras el backfill, la migración `*_clinical_ciphertext_checks` impide que vuelvan.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
_NONCE_BYTES = 12
_KEY_BYTES = 32
# Lo que muestra un `Sealed` si alguien lo interpola en un log, un correo o un f-string.
REDACTED = "[INFORMACIÓN MÉDICA CONFIDENCIAL]"


class ClinicalCryptoError(Exception):
    """Clave mal configurada, texto cifrado manipulado o cifrado con una clave que no está en
    el llavero. El mensaje nunca lleva contenido clínico."""


def _decode_key(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ClinicalCryptoError("La clave clínica no es base64 válido.") from exc
    if len(key) != _KEY_BYTES:
        raise ClinicalCryptoError(
            f"La clave clínica debe tener {_KEY_BYTES} bytes (AES-256); tiene {len(key)}."
        )
    return key


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def generate_key() -> str:
    """Una clave nueva en base64, lista para `CLINICAL_DATA_ENCRYPTION_KEY`."""
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode()


@dataclass(frozen=True)
class Keyring:
    active_kid: str
    keys: dict[str, AESGCM]

    @classmethod
    def from_config(cls, active: str, previous: str = "") -> Keyring:
        active_key = _decode_key(active)
        keys = {key_id(active_key): AESGCM(active_key)}
        for raw in previous.split(","):
            if raw.strip():
                k = _decode_key(raw)
                keys.setdefault(key_id(k), AESGCM(k))
        return cls(active_kid=key_id(active_key), keys=keys)

    def encrypt(self, plaintext: str, *, field: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ct = self.keys[self.active_kid].encrypt(nonce, plaintext.encode(), field.encode())
        blob = base64.urlsafe_b64encode(nonce + ct).decode()
        return f"{PREFIX}{self.active_kid}:{blob}"

    def decrypt(self, ciphertext: str, *, field: str) -> str:
        kid, blob = _split(ciphertext)
        aead = self.keys.get(kid)
        if aead is None:
            raise ClinicalCryptoError(f"Texto cifrado con una clave desconocida ({kid}).")
        try:
            raw = base64.urlsafe_b64decode(blob)
            return aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], field.encode()).decode()
        except (InvalidTag, binascii.Error, ValueError) as exc:
            raise ClinicalCryptoError(
                f"No se pudo descifrar {field}: texto manipulado o de otra columna."
            ) from exc


def _split(ciphertext: str) -> tuple[str, str]:
    kid, sep, blob = ciphertext.removeprefix(PREFIX).partition(":")
    if not ciphertext.startswith(PREFIX) or not sep:
        raise ClinicalCryptoError("Formato de texto cifrado inválido.")
    return kid, blob


def is_ciphertext(value: str) -> bool:
    return value.startswith(PREFIX)


def ciphertext_kid(value: str) -> str:
    return _split(value)[0]


@lru_cache
def keyring() -> Keyring:
    # Import diferido: config importa cosas de core y no queremos ciclos al importar el tipo.
    from src.core.config import settings

    return Keyring.from_config(
        settings.CLINICAL_DATA_ENCRYPTION_KEY, settings.CLINICAL_DATA_ENCRYPTION_PREVIOUS_KEYS
    )


class Sealed:
    """Valor clínico tal como sale de la base: cifrado y opaco.

    Es lo que el ORM devuelve para una columna `EncryptedText` en lugar de un `str`. No se
    convierte solo a texto: `str()`, `repr()` y los f-strings dan `REDACTED`, y un esquema
    Pydantic con un campo `str` normal lo rechaza (500 en vez de fuga). La única forma de ver
    el contenido es `reveal()`, que solo llaman los tipos de `src/schemas/clinical.py` tras
    comprobar el permiso, y los pocos servicios que lo necesitan para operar.
    """

    __slots__ = ("field", "ciphertext", "_legacy_plaintext")

    def __init__(self, field: str, ciphertext: str | None, legacy_plaintext: str | None = None):
        self.field = field
        self.ciphertext = ciphertext
        self._legacy_plaintext = legacy_plaintext

    @classmethod
    def from_db(cls, field: str, value: str) -> Sealed:
        if is_ciphertext(value):
            return cls(field, value)
        # Fila anterior al backfill: sigue en claro en la base, pero en memoria se trata igual.
        return cls(field, None, legacy_plaintext=value)

    def reveal(self) -> str:
        if self.ciphertext is None:
            return self._legacy_plaintext or ""
        return keyring().decrypt(self.ciphertext, field=self.field)

    def ciphertext_for(self, field: str) -> str:
        """Texto cifrado para guardarlo en `field`. Si viene de la misma columna se reutiliza
        tal cual (derivar copia el motivo del padre al hijo); de otra columna se re-cifra,
        porque el AAD ata cada texto a su columna."""
        if self.ciphertext is not None and self.field == field:
            return self.ciphertext
        return keyring().encrypt(self.reveal(), field=field)

    def __eq__(self, other: object) -> bool:
        # Lo usa el ORM para detectar cambios. Comparar contra un str devolvería el contenido
        # por canal lateral y además no tiene sentido (el nonce cambia en cada cifrado).
        if not isinstance(other, Sealed):
            return NotImplemented
        return (self.field, self.ciphertext, self._legacy_plaintext) == (
            other.field,
            other.ciphertext,
            other._legacy_plaintext,
        )

    def __hash__(self) -> int:
        return hash((self.field, self.ciphertext, self._legacy_plaintext))

    def __str__(self) -> str:
        return REDACTED

    __repr__ = __str__


def reveal(value: Sealed | str | None) -> str | None:
    """Texto en claro de un valor clínico. `str` es un valor recién escrito en esta petición
    (aún no releído de la base), que ya estaba en claro en memoria."""
    if value is None or isinstance(value, str):
        return value
    return value.reveal()
