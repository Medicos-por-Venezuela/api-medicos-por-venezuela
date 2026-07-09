"""Capa de negocio para doctors.

Al registrar, el backend **verifica la credencial** contra el registro oficial que
corresponde al tipo profesional elegido: Médico -> SACS, Psicólogo -> FPV. `verified`
queda en True solo si la cédula es válida en ese registro; en cualquier otro caso
(tipo desconocido, servicio caído, no encontrado) queda en False (fail-closed).
"""

import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from src.core.errors import BadRequestError, NotFoundError
from src.models.doctor import Doctor
from src.models.professional_type import ProfessionalType
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.schemas.doctor import DoctorCreate, DoctorMeResponse, DoctorSelfUpdate, DoctorUpdate
from src.services import audit
from src.services import psicologo as psicologo_service
from src.services import sacs as sacs_service

# Roles de `users` que corresponden a un médico (legacy `specialist` -> doctor).
_DOCTOR_PROFILE_ROLES = {"doctor", "specialist"}

# Un médico cuenta como "online" si marcó presencia hace menos de esto (igual criterio
# que el panel/admin del frontend: last_seen_at < 3 min).
_ONLINE_WINDOW = timedelta(minutes=3)


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


async def list_doctor_pool(
    session: AsyncSession,
    *,
    skip: int = 0,
    limit: int = 20,
    specialty_id: uuid.UUID | None = None,
    professional_type_id: uuid.UUID | None = None,
    online: bool | None = None,
    exclude_user_id: uuid.UUID | None = None,
) -> tuple[list[dict], int]:
    """Pool de médicos para referir/agendar: cruza doctors con users (para el estado
    online desde last_seen_at y el teléfono de contacto) y pagina. Devuelve (filas, total).

    Solo médicos que pueden atender: status == 1 (excluye baja=0 y expulsado=2) y no
    borrados. El inner join con users descarta los mocks legacy sin user_id. `online`:
    True = logeado (< 3 min), False = offline, None = todos. Ordena los online primero.
    `exclude_user_id`: quita al propio médico que consulta (no se refiere a sí mismo).
    """
    threshold = datetime.now(UTC) - _ONLINE_WINDOW
    online_expr = Profile.last_seen_at >= threshold
    # Teléfono para el enlace de WhatsApp: el de doctors o, si falta, el whatsapp de la cuenta.
    phone_expr = func.coalesce(Doctor.phone, Profile.whatsapp_number)

    base = (
        select(
            Doctor.id,
            Doctor.full_name,
            Doctor.specialty_id,
            Doctor.professional_type_id,
            phone_expr.label("phone"),
            online_expr.label("online"),
        )
        .join(Profile, Doctor.user_id == Profile.id)
        .where(Doctor.deleted_at.is_(None), Doctor.status == 1)
    )
    if exclude_user_id is not None:
        base = base.where(Doctor.user_id != exclude_user_id)
    if specialty_id is not None:
        base = base.where(Doctor.specialty_id == specialty_id)
    if professional_type_id is not None:
        base = base.where(Doctor.professional_type_id == professional_type_id)
    if online is True:
        base = base.where(online_expr)
    elif online is False:
        base = base.where(or_(Profile.last_seen_at.is_(None), Profile.last_seen_at < threshold))

    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0

    page = (
        base.order_by(Profile.last_seen_at.desc().nulls_last(), Doctor.full_name)
        .offset(skip)
        .limit(limit)
    )
    rows = (await session.execute(page)).all()
    items = [
        {
            "id": r.id,
            "full_name": r.full_name,
            "specialty_id": r.specialty_id,
            "professional_type_id": r.professional_type_id,
            "phone": r.phone,
            "online": bool(r.online),
        }
        for r in rows
    ]
    return items, total


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


async def update_doctor(
    session: AsyncSession,
    doctor_id: uuid.UUID,
    data: DoctorUpdate,
    actor_user_id: uuid.UUID | None = None,
) -> Doctor:
    doctor = await get_doctor(session, doctor_id)
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(doctor, field, value)
    await _sync_user_from_doctor(session, doctor)
    await audit.log_action(
        session,
        action="doctor.updated",
        actor_user_id=actor_user_id,
        resource="doctors",
        resource_id=doctor.id,
        metadata={"fields": sorted(changes)},
    )
    await session.commit()
    await session.refresh(doctor)
    return doctor


async def delete_doctor(
    session: AsyncSession, doctor_id: uuid.UUID, actor_user_id: uuid.UUID | None = None
) -> None:
    """Baja lógica (soft delete): marca deleted_at, no borra la fila."""
    doctor = await get_doctor(session, doctor_id)
    doctor.deleted_at = func.now()
    await audit.log_action(
        session,
        action="doctor.deleted",
        actor_user_id=actor_user_id,
        resource="doctors",
        resource_id=doctor.id,
    )
    await session.commit()


# --- Perfil propio del médico (self-service) ---------------------------------
# El recurso se resuelve SIEMPRE desde el `user_id` del JWT (nunca de la URL/payload),
# así que es IDOR-safe por construcción: nadie puede leer/editar el perfil de otro.


async def _specialty_name(session: AsyncSession, specialty_id: uuid.UUID | None) -> str | None:
    if specialty_id is None:
        return None
    return await session.scalar(select(Specialty.name).where(Specialty.id == specialty_id))


async def _my_doctor_row(session: AsyncSession, user_id: uuid.UUID) -> Doctor | None:
    """Fila `doctors` ligada a la cuenta (1:1), si existe y no está borrada."""
    stmt = (
        select(Doctor)
        .where(Doctor.user_id == user_id, Doctor.deleted_at.is_(None))
        .order_by(Doctor.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _my_doctor_profile(session: AsyncSession, user_id: uuid.UUID) -> Profile:
    """Cuenta (`users`) del llamante, solo si es un médico. 404 en caso contrario
    (un paciente/admin sin fila en `doctors` no tiene 'perfil de médico')."""
    profile = await session.get(Profile, user_id)
    if profile is None or profile.role not in _DOCTOR_PROFILE_ROLES:
        raise NotFoundError("No tienes un perfil de médico.")
    return profile


def _me_from_doctor(
    user_id: uuid.UUID, doctor: Doctor, specialty_name: str | None
) -> DoctorMeResponse:
    return DoctorMeResponse(
        source="doctor",
        user_id=user_id,
        doctor_id=doctor.id,
        cedula=doctor.cedula,
        full_name=doctor.full_name,
        license=doctor.license,
        specialty_id=doctor.specialty_id,
        specialty=specialty_name,
        verified=doctor.verified,
    )


def _me_from_profile(profile: Profile) -> DoctorMeResponse:
    return DoctorMeResponse(
        source="user",
        user_id=profile.id,
        doctor_id=None,
        cedula=None,  # users no guarda cédula
        full_name=profile.full_name,
        license=profile.medical_license,
        specialty_id=None,  # users guarda el nombre de la especialidad, no el id
        specialty=profile.specialty,
        verified=profile.verified,
    )


async def get_my_profile(session: AsyncSession, user_id: uuid.UUID) -> DoctorMeResponse:
    """Perfil del médico autenticado. Prefiere la fila en `doctors`; si no existe,
    cae a su cuenta en `users` (médicos que entraron por Google/`finalize-role`)."""
    doctor = await _my_doctor_row(session, user_id)
    if doctor is not None:
        return _me_from_doctor(
            user_id, doctor, await _specialty_name(session, doctor.specialty_id)
        )
    return _me_from_profile(await _my_doctor_profile(session, user_id))


async def _update_my_doctor_row(
    session: AsyncSession, user_id: uuid.UUID, doctor: Doctor, data: DoctorSelfUpdate
) -> DoctorMeResponse:
    fields = data.model_dump(exclude_unset=True)
    new_cedula = fields.pop("cedula", None)
    for field, value in fields.items():
        setattr(doctor, field, value)
    # Cambiar la cédula re-verifica contra el registro oficial de su tipo y
    # recalcula `verified` (fail-closed si ya no valida).
    if new_cedula is not None and new_cedula != doctor.cedula:
        doctor.cedula = new_cedula
        doctor.verified = await _verify_credential(
            session, doctor.professional_type_id, new_cedula
        )
    await _sync_user_from_doctor(session, doctor)
    await session.commit()
    await session.refresh(doctor)
    return _me_from_doctor(user_id, doctor, await _specialty_name(session, doctor.specialty_id))


async def _update_my_profile_row(
    session: AsyncSession, user_id: uuid.UUID, data: DoctorSelfUpdate
) -> DoctorMeResponse:
    profile = await _my_doctor_profile(session, user_id)
    fields = data.model_dump(exclude_unset=True)
    # En la fuente `users` no hay cédula ni tipo profesional que verificar: rechazo.
    if fields.get("cedula") is not None:
        raise BadRequestError("No puedes editar la cédula desde este perfil.")
    if fields.get("full_name") is not None:
        profile.full_name = fields["full_name"]
    if "license" in fields:
        profile.medical_license = fields["license"]
    if "specialty_id" in fields:
        profile.specialty = await _specialty_name(session, fields["specialty_id"])
    await session.commit()
    await session.refresh(profile)
    return _me_from_profile(profile)


async def update_my_profile(
    session: AsyncSession, user_id: uuid.UUID, data: DoctorSelfUpdate
) -> DoctorMeResponse:
    """Auto-edición del perfil propio. Sobre la fila `doctors` cambiar la cédula
    re-verifica SACS/FPV; sobre la cuenta `users` la cédula no es editable (400)."""
    doctor = await _my_doctor_row(session, user_id)
    if doctor is not None:
        return await _update_my_doctor_row(session, user_id, doctor, data)
    return await _update_my_profile_row(session, user_id, data)
