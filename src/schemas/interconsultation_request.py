"""Esquemas Pydantic de la interconsulta asíncrona.

Estos modelos **son** la frontera de datos del feature, no un envoltorio de ella: lo que el
especialista puede ver antes y después de tomar un caso está definido por qué campos declara
cada clase. Un campo que no está acá no puede escaparse aunque el servicio lo traiga.
Ver tasks/interconsulta-asincrona/spec.md.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DoctorContact(BaseModel):
    """Identidad y contacto de un médico, para que dos colegas se hablen FUERA de la plataforma.

    Nunca describe a un paciente: acá solo viajan datos de médicos.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    whatsapp_number: str | None = None
    email: str | None = None


class InterconsultationRequestCreate(BaseModel):
    """Alta de una solicitud. `mode` decide qué campo acompaña:

    - `specialty` (el principal): `specialty_id`, y se difunde a todos los médicos de esa
      especialidad.
    - `doctor`: `target_doctor_id`, y solo le llega a él. La especialidad se deriva de su ficha,
      no la manda el cliente.
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: uuid.UUID
    mode: Literal["specialty", "doctor"] = "specialty"
    specialty_id: uuid.UUID | None = None
    target_doctor_id: uuid.UUID | None = None
    chief_complaint: str = Field(..., min_length=10, max_length=2000)
    clinical_notes: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _campo_segun_modo(self) -> "InterconsultationRequestCreate":
        """El mismo invariante que impone la BD (ck_interconsultation_requests_target), acá
        arriba para devolver un 422 explicativo en vez de un 409 de constraint."""
        if self.mode == "specialty":
            if self.specialty_id is None:
                raise ValueError("En modo 'specialty' hace falta specialty_id.")
            if self.target_doctor_id is not None:
                raise ValueError("En modo 'specialty' no se manda target_doctor_id.")
        else:
            if self.target_doctor_id is None:
                raise ValueError("En modo 'doctor' hace falta target_doctor_id.")
            if self.specialty_id is not None:
                raise ValueError(
                    "En modo 'doctor' la especialidad se deriva del médico elegido; "
                    "no mandes specialty_id."
                )
        return self


class InterconsultationRequestInbox(BaseModel):
    """Lo que ve el ESPECIALISTA **antes** de tomar el caso. Anonimizado.

    Esta clase ES la frontera de datos, no un filtro sobre ella: los campos prohibidos no están
    declarados, así que no pueden escaparse aunque la query los traiga. Antes de agregar un campo
    acá, preguntate si el especialista lo necesita para decidir si toma el caso.

    NUNCA: nombre, cédula, teléfono, correo, zona ni descripción del paciente — el paciente no es
    usuario de la plataforma y su relación es con su médico. Tampoco la identidad del médico que
    pide: que el caso se elija por el caso, no por quién pregunta.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    specialty_id: uuid.UUID
    specialty_name: str | None = None
    chief_complaint: str
    clinical_notes: str | None = None
    # Solo el RANGO etario, nunca la fecha de nacimiento: alcanza para valorar el caso.
    patient_age_range: str | None = None
    # Si la solicitud venía dirigida a este especialista en concreto (modo 'doctor').
    dirigida_a_mi: bool = False
    created_at: datetime


class InterconsultationRequestTaken(BaseModel):
    """Lo que recibe el especialista **al tomar** el caso: el contacto del médico TRATANTE.

    Es el objetivo de todo el flujo — que los dos médicos se hablen fuera de la plataforma. Se
    suma la identidad del tratante, nunca la del paciente.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    taken_at: datetime
    specialty_name: str | None = None
    chief_complaint: str
    clinical_notes: str | None = None
    patient_age_range: str | None = None
    requesting_doctor: DoctorContact


class InterconsultationRequestClose(BaseModel):
    """Cierre del caso por el médico tratante (nota opcional)."""

    model_config = ConfigDict(extra="forbid")

    closing_note: str | None = Field(default=None, max_length=2000)


class InterconsultationRequestResponse(BaseModel):
    """Lo que ve el MÉDICO TRATANTE de su propia solicitud: todo lo suyo.

    Incluye el nombre de su paciente (es su paciente) y, si el caso fue tomado, la identidad y
    el contacto del especialista — que es el objetivo de todo el flujo.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    patient_name: str | None = None
    mode: str
    specialty_id: uuid.UUID
    specialty_name: str | None = None
    chief_complaint: str
    clinical_notes: str | None = None
    status: str
    # Cuántos especialistas elegibles fueron notificados. Alimenta el "se avisó a N colegas"
    # de la UI y, si alguien dice "no me llegó", distingue el fallo de envío del "no eras
    # destinatario".
    notified_count: int
    created_at: datetime
    taken_at: datetime | None = None
    closed_at: datetime | None = None
    cancelled_at: datetime | None = None
    # En modo 'doctor', a quién se dirigió — con su teléfono: el tratante lo eligió, puede
    # llamarlo sin esperar a que tome el caso.
    target_doctor: DoctorContact | None = None
    # Quién la tomó. None mientras siga abierta.
    taken_by: DoctorContact | None = None
