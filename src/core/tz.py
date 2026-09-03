"""Hora de Venezuela: la única definición del huso con que la API le habla al usuario.

Todo se guarda en UTC (`timestamptz`) y eso no cambia. Lo que necesita una zona es la SALIDA
que lee una persona: la fecha de la cita en un correo, las columnas de fecha de un reporte en
Excel, y el "hasta el día X" de un filtro por rango (que es un día de calendario venezolano,
no una ventana UTC).

Venezuela es **UTC-4 fijo** (sin horario de verano desde 2016), así que es un offset constante
y no una regla con historia. Se modela con `timezone(timedelta(hours=-4))` en vez de
`ZoneInfo("America/Caracas")` a propósito: `zoneinfo` depende de la base de datos de husos del
sistema, que en Windows no existe y obliga a instalar el paquete `tzdata` — el entorno de
desarrollo de este equipo es Windows y `zoneinfo` ahí falla en import time.
"""

from datetime import UTC, date, datetime, time, timedelta, timezone

VET = timezone(timedelta(hours=-4))


def to_local(dt: datetime | None) -> datetime | None:
    """UTC -> hora de Venezuela, **sin** tzinfo.

    Se descarta el tzinfo porque los consumidores son formateadores que ya asumen hora local:
    `strftime` en los correos y `xlsxwriter`, que directamente rechaza datetimes con zona (una
    celda de Excel no tiene huso; lleva el número y el formato).
    """
    if dt is None:
        return None
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(VET).replace(tzinfo=None)


def day_bounds(desde: date | None, hasta: date | None) -> tuple[datetime | None, datetime | None]:
    """`[desde, hasta]` como días de calendario venezolanos -> instantes UTC `[inicio, fin)`.

    `hasta` es **inclusivo**: se traduce al comienzo del día siguiente, porque un admin que
    filtra "hasta el 3 de septiembre" espera ver lo del 3 de septiembre. Comparar contra la
    medianoche del 3 excluiría el día entero, que es el bug clásico de estos filtros.
    """
    start = datetime.combine(desde, time.min, tzinfo=VET).astimezone(UTC) if desde else None
    end = (
        (datetime.combine(hasta, time.min, tzinfo=VET) + timedelta(days=1)).astimezone(UTC)
        if hasta
        else None
    )
    return start, end
