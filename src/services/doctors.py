"""Capa de negocio para doctors.

Al registrar, el backend **verifica la credencial** contra el registro oficial que
corresponde al tipo profesional elegido: Médico -> SACS, Psicólogo -> FPV. `verified`
queda en True solo si la cédula es válida en ese registro; en cualquier otro caso
(tipo desconocido, servicio caído, no encontrado) queda en False (fail-closed).
"""

import unicodedata
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from src.core.errors import BadRequestError, NotFoundError
from src.models.doctor import Doctor
from src.models.professional_type import ProfessionalType
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.schemas.doctor import DoctorCreate, DoctorUpdate
from src.services import psicologo as psicologo_service
from src.services import sacs as sacs_service


def _normalize(text: str) -> str:
    """minúsculas y sin acentos: 'Médico' -> 'medico', 'Psicólogo' -> 'psicologo'."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


async def _verified_in_sacs(cedula: str) -> bool:
    """El SACS confirma que la cédula corresponde a un médico registrado."""
    result = await sacs_service.verificar_sacs(cedula)
    return bool(result.encontrado and result.es_medico)


async def _verified_in_fpv(cedula: str) -> bool:
    """La FPV confirma que la cédula corresponde a un psicólogo colegiado."""
    result = await psicologo_service.verificar_psicologo(cedula)
    return bool(result.encontrado)


# Tipo profesional (normalizado, sin acentos) -> registro oficial que lo valida.
# Añadir un tipo verificable = una entrada más, sin tocar la lógica de ruteo.
_CREDENTIAL_VERIFIERS: dict[str, Callable[[str], Awaitable[bool]]] = {
    "medico": _verified_in_sacs,
    "psicologo": _verified_in_fpv,
}


async def _verify_credential(
    session: AsyncSession, professional_type_id: uuid.UUID | None, cedula: str
) -> bool:
    """True si la cédula está en el registro oficial de su tipo profesional.

    Fail-closed: sin tipo, tipo inexistente o tipo sin registro verificable
    (p. ej. nutricionista) -> False.
    """
    if professional_type_id is None:
        return False
    ptype = await session.get(ProfessionalType, professional_type_id)
    if ptype is None:
        return False
    verify = _CREDENTIAL_VERIFIERS.get(_normalize(ptype.name))
    return await verify(cedula) if verify else False


async def _sync_user_from_doctor(session: AsyncSession, doctor: Doctor) -> None:
    """Propaga specialty/country/medical_license/whatsapp_number de doctors a la
    cuenta (users/profiles) ligada, si existe.

    Decisión de producto: estos 4 campos siguen viviendo también en `users` (los
    completa `set_my_role` para médicos que entran por Google/`/elegir-rol`, un
    camino de registro distinto que no pasa por esta tabla). Para los médicos que
    SÍ pasan por acá (registro con verificación SACS/FPV), `doctors` es la fuente
    de verdad; sin este sync quedaban NULL en `users` y todo lo que lee
    `profiles.specialty` (panel médico, matching de la cola, admin) no los veía.
    """
    if doctor.user_id is None:
        return
    user = await session.get(Profile, doctor.user_id)
    if user is None:
        return
    specialty_name = None
    if doctor.specialty_id:
        specialty_name = await session.scalar(
            select(Specialty.name).where(Specialty.id == doctor.specialty_id)
        )
    user.specialty = specialty_name
    user.country = doctor.country_of_residence
    user.medical_license = doctor.license
    user.whatsapp_number = doctor.phone


async def list_doctors(
    session: AsyncSession,
    skip: int = 0,
    limit: int = 100,
    status: int | None = None,
) -> list[Doctor]:
    stmt = select(Doctor).where(Doctor.deleted_at.is_(None))
    if status is not None:
        stmt = stmt.where(Doctor.status == status)
    stmt = stmt.order_by(Doctor.created_at.desc()).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_doctor(session: AsyncSession, doctor_id: uuid.UUID) -> Doctor:
    doctor = await session.get(Doctor, doctor_id)
    if doctor is None or doctor.deleted_at is not None:
        raise NotFoundError("Médico no encontrado.")
    return doctor


async def create_doctor(session: AsyncSession, data: DoctorCreate) -> Doctor:
    """Registra un médico. `verified` se decide contra SACS/FPV; `status` = 1 (activo)."""
    # Honeypot: si el campo trampa llegó con valor, es un bot. Rechazo genérico.
    if data.website:
        raise BadRequestError("Solicitud inválida.")
    verified = await _verify_credential(session, data.professional_type_id, data.cedula)
    # Liga el doctor a su cuenta (users) por email, si ya existe. El signup crea la cuenta
    # justo antes de este POST, así que normalmente la resuelve. Server-side (no lo manda el
    # cliente) para evitar IDOR.
    user_id = (
        await session.execute(select(Profile.id).where(Profile.email == data.email))
    ).scalar_one_or_none()
    doctor = Doctor(**data.model_dump(exclude={"website"}), verified=verified, user_id=user_id)
    session.add(doctor)
    await session.flush()
    await _sync_user_from_doctor(session, doctor)
    await session.commit()
    await session.refresh(doctor)
    return doctor


async def update_doctor(session: AsyncSession, doctor_id: uuid.UUID, data: DoctorUpdate) -> Doctor:
    doctor = await get_doctor(session, doctor_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(doctor, field, value)
    await _sync_user_from_doctor(session, doctor)
    await session.commit()
    await session.refresh(doctor)
    return doctor


async def delete_doctor(session: AsyncSession, doctor_id: uuid.UUID) -> None:
    """Baja lógica (soft delete): marca deleted_at, no borra la fila."""
    doctor = await get_doctor(session, doctor_id)
    doctor.deleted_at = func.now()
    await session.commit()
