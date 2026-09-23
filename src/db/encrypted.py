"""Tipo de columna `EncryptedText`: cifra al escribir, devuelve `Sealed` al leer.

La columna en Postgres sigue siendo `text`; lo que cambia es lo que viaja. Al escribir, un
`str` se cifra con la clave activa y un `Sealed` se reutiliza (o se re-cifra si viene de otra
columna). Al leer, NUNCA se descifra: el ORM entrega un `Sealed` y quien quiera el texto tiene
que pedirlo con `reveal()`. Así, cargar una consulta para cambiarle el estado (lo que hace un
admin) no descifra nada.

No uses estas columnas en WHERE/ORDER BY/ILIKE: el patrón de búsqueda se cifraría con un nonce
aleatorio y nunca coincidiría. Solo `IS NULL` / `IS NOT NULL` tienen sentido.
"""

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from src.core.clinical_crypto import Sealed, keyring


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, field: str) -> None:
        """`field` = "tabla.columna"; es el AAD del cifrado (ver `clinical_crypto`)."""
        super().__init__()
        self.field = field

    def process_bind_param(self, value: Sealed | str | None, dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, Sealed):
            return value.ciphertext_for(self.field)
        return keyring().encrypt(value, field=self.field)

    def process_result_value(self, value: str | None, dialect) -> Sealed | None:
        if value is None:
            return None
        return Sealed.from_db(self.field, value)
