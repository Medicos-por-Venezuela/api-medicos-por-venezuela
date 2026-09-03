"""Esquemas Pydantic de los reportes (vista previa en JSON antes de exportar a Excel).

La vista previa y el `.xlsx` salen de la misma consulta y de las mismas filas ya presentadas
(ver `src/services/reports.py`), así que el contrato es genérico: `columns` describe la tabla y
`rows` son diccionarios con esas mismas claves. El frontend pinta lo que venga sin conocer los
campos de médico ni de paciente, y añadir una columna al reporte no obliga a tocar el cliente.
"""

from pydantic import BaseModel, ConfigDict, Field


class ReportColumn(BaseModel):
    """Descripción de una columna del reporte, para que el cliente arme la tabla."""

    model_config = ConfigDict(from_attributes=True)

    key: str = Field(description="Clave con la que el valor aparece en cada fila de `rows`.")
    header: str = Field(description="Cabecera visible (español), la misma que sale en el Excel.")
    kind: str = Field(description="'text' o 'datetime' (formato de fecha/hora en el Excel).")


class ReportPreview(BaseModel):
    """Una página del reporte + el total de filas que cumplen el filtro.

    `total` es la cifra que se exportará; `rows` es solo la página que se está mirando. Que la
    vista previa muestre 50 filas y el archivo traiga 2.847 no es una inconsistencia: es la
    misma consulta con y sin `limit`.
    """

    columns: list[ReportColumn]
    rows: list[dict]
    total: int = Field(description="Filas que cumplen el filtro (las que traerá el Excel).")
    filters: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Filtros aplicados, ya legibles, como pares [etiqueta, valor]. Son los mismos que "
            "se imprimen en la portada del archivo exportado."
        ),
    )
