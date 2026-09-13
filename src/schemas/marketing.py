"""Esquemas Pydantic de las encuestas de marketing (envío público de respuestas).

El listado y la exportación del panel no tienen esquema propio: devuelven el `ReportPreview`
genérico de los reportes (ver `src/schemas/report.py`), así el panel pinta las respuestas con la
misma tabla que ya usa para médicos y pacientes.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# Slug de cada encuesta: el mismo en la URL pública (/encuesta/<slug>), en el endpoint y en la
# columna `survey`, para no mantener equivalencias entre capas.
SurveySlug = Literal["psicologos", "especialistas", "medicos-generales"]

# Código de una opción marcada (p. ej. 'atender_pacientes'). Aquí solo se acota la forma: QUÉ
# códigos acepta cada encuesta lo decide el servicio, porque depende de la encuesta, que llega por
# la ruta y no en el cuerpo.
OptionCode = Annotated[str, Field(min_length=1, max_length=40)]


class SurveyResponseCreate(BaseModel):
    """Lo que manda el formulario público.

    Todos los campos existen para las tres encuestas; los que una encuesta no pregunta (la zona
    horaria en médicos generales, el "rol más activo" en las otras dos) se descartan en el
    servicio en vez de guardarse a medias.
    """

    model_config = ConfigDict(extra="forbid")

    # Llega en el enlace del correo masivo: se valida el formato, no que sea de quien responde.
    email: EmailStr
    roles: list[OptionCode] = Field(..., min_length=1, max_length=10)
    role_active_detail: str | None = Field(default=None, max_length=500)
    role_other_detail: str | None = Field(default=None, max_length=500)
    moments: list[OptionCode] = Field(default_factory=list, max_length=10)
    days: list[OptionCode] = Field(default_factory=list, max_length=10)
    weekly_hours: OptionCode | None = None
    availability_notes: str | None = Field(default=None, max_length=2000)
    timezone: OptionCode | None = None
    timezone_other: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    # Honeypot anti-bot, el mismo que el registro de médicos (`DoctorCreate.website`): el
    # formulario lo renderiza oculto y un humano no lo llena. Con valor, se rechaza el envío.
    website: str | None = Field(default=None, max_length=200)


class SurveyResponseReceipt(BaseModel):
    """Acuse del envío.

    No devuelve las respuestas: el endpoint es público y el correo no está verificado, así que
    devolverlas sería enseñarle a cualquiera lo que contestó otra persona con solo escribir su
    correo. `created_at` distinto de `updated_at` significa que se actualizó una respuesta previa.
    """

    survey: SurveySlug
    created_at: datetime
    updated_at: datetime
