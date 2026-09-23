"""Campos clínicos en los esquemas de SALIDA: se descifran solo con permiso explícito.

Un campo `ClinicalSummary` / `ClinicalNote` vale `null` salvo que el router valide el esquema
con un contexto que lo conceda:

    ConsultationResponse.model_validate(c, context=clinical_context(grant))

Sin contexto, o con un permiso que no alcanza ese nivel, sale `null`. Es fail-closed: un
endpoint nuevo que olvide pedir permiso devuelve el dato enmascarado, no en claro.

Dos niveles, porque no todos los que ven un caso necesitan lo mismo:
- SUMMARY: el motivo y los antecedentes/alergias del paciente. Lo ve el propio paciente, el
  médico cuya cola incluye el caso (para decidir si lo toma) y el equipo tratante.
- NOTES: lo que escribe el médico (notas internas y clínicas, notas de cierre, motivo de
  derivación, notas de interconsulta). Solo el equipo tratante.

El admin no recibe ninguno: gestiona estado, asignación y prioridad, no el contenido clínico.
Las respuestas llevan `clinical_access` para que el frontend distinga "sin permiso" de "vacío".
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, ValidationInfo, field_validator

from src.core.clinical_crypto import ClinicalCryptoError, Sealed, reveal

logger = logging.getLogger("mpv.api")

_CONTEXT_KEY = "clinical_grant"


class Tier(StrEnum):
    SUMMARY = "summary"
    NOTES = "notes"


# Por qué se concede el acceso. Va al audit_log (`metadata.via`).
GrantReason = Literal["patient_owner", "assigned_doctor", "queue_scope", "interconsultation"]


@dataclass(frozen=True)
class ClinicalGrant:
    reason: GrantReason
    tiers: frozenset[Tier]


def summary_grant(reason: GrantReason) -> ClinicalGrant:
    return ClinicalGrant(reason, frozenset({Tier.SUMMARY}))


def treating_grant(reason: GrantReason) -> ClinicalGrant:
    return ClinicalGrant(reason, frozenset({Tier.SUMMARY, Tier.NOTES}))


def clinical_context(grant: ClinicalGrant | None) -> dict[str, Any]:
    return {_CONTEXT_KEY: grant}


def _grant(info: ValidationInfo) -> ClinicalGrant | None:
    return (info.context or {}).get(_CONTEXT_KEY)


def _clinical_validator(tier: Tier):
    def validate(value: Any, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        grant = _grant(info)
        if grant is None or tier not in grant.tiers:
            return None
        if not isinstance(value, Sealed | str):
            raise ValueError("Un campo clínico solo acepta texto o Sealed.")
        try:
            return reveal(value)
        except ClinicalCryptoError:
            # Una fila que no descifra (manipulada, o cifrada con una clave que ya no está en
            # el llavero) no debe tumbar el panel entero. Se loguea sin contenido y va en null.
            logger.error("Campo clínico indescifrable (%s)", getattr(value, "field", "?"))
            return None

    return BeforeValidator(validate)


ClinicalSummary = Annotated[str | None, _clinical_validator(Tier.SUMMARY)]
ClinicalNote = Annotated[str | None, _clinical_validator(Tier.NOTES)]


class ClinicalAccessMixin(BaseModel):
    """Añade `clinical_access` a una respuesta: `full`, `summary` o `none`. El frontend pinta
    "[Información médica confidencial]" cuando un campo viene en null con `none`/`summary`."""

    clinical_access: Literal["full", "summary", "none"] = Field(
        default="none",
        validate_default=True,
        description="Nivel de acceso a los campos clínicos con el que se generó esta respuesta. "
        "`none`: los campos clínicos van en null por permiso, no porque estén vacíos.",
    )

    @field_validator("clinical_access", mode="before")
    @classmethod
    def _from_context(cls, _: Any, info: ValidationInfo) -> str:
        grant = _grant(info)
        if grant is None:
            return "none"
        return "full" if Tier.NOTES in grant.tiers else "summary"
