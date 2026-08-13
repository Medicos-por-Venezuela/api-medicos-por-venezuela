"""Capa de negocio para doctors.

Al registrar, el backend **verifica la credencial** contra el registro oficial que
corresponde al tipo profesional elegido: Médico -> SACS, Psicólogo -> FPV. `verified`
queda en True solo si la cédula es válida en ese registro; en cualquier otro caso
(tipo desconocido, servicio caído, no encontrado) queda en False (fail-closed).
"""

import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from src.core.errors import BadRequestError, ConflictError, NotFoundError, UnprocessableError
from src.models.doctor import Doctor
from src.models.professional_type import ProfessionalType
from src.models.profile import Profile
from src.models.specialty import Specialty
from src.schemas.doctor import DoctorCreate, DoctorMeResponse, DoctorSelfUpdate, DoctorUpdate
from src.services import audit
from src.services import psicologo as psicologo_service
from src.services import sacs as sacs_service
from src.services import specialties as specialties_service

# Roles de `users` que corresponden a un médico (legacy `specialist` -> doctor).
_DOCTOR_PROFILE_ROLES = {"doctor", "specialist"}

# Ventana de "online" por last_seen_at (< 3 min). La reutiliza services/stats.py como única fuente
# de verdad del KPI doctors_online del dashboard admin (el pool de médicos ya usa Presence).
ONLINE_WINDOW = timedelta(minutes=3)


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
    # La FK manda; el nombre es su copia desnormalizada. Nunca uno sin el otro.
    user.specialty_id = doctor.specialty_id
    user.specialty = await specialties_service.name_for_id(session, doctor.specialty_id)
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
    search: str | None = None,
    online: bool | None = None,
    online_user_ids: list[uuid.UUID] | None = None,
    exclude_user_id: uuid.UUID | None = None,
) -> tuple[list[dict], int]:
    """Pool de médicos para referir/agendar (paginado). Devuelve (filas, total). NO trae el
    teléfono: el número se revela (y se audita) aparte, con `reveal_doctor_contact`.

    Solo médicos que pueden atender: status == 1 (excluye baja=0 y expulsado=2) y no borrados.
    El inner join con users descarta los mocks legacy sin user_id. Filtros: `search` (nombre,
    ILIKE), `specialty_id`, `professional_type_id`. El estado "online" lo sabe el cliente
    (Presence) y lo pasa como `online_user_ids`: `online=True` -> user_id IN esa lista;
    `online=False` -> NOT IN; `None` -> sin filtro (paginación server-side correcta).
    `exclude_user_id`: quita al propio médico que consulta.
    """
    base = (
        select(
            Doctor.id,
            Doctor.user_id,
            Doctor.full_name,
            Doctor.specialty_id,
            Doctor.professional_type_id,
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
    if search:
        base = base.where(Doctor.full_name.ilike(f"%{search}%"))
    if online is not None:
        ids = online_user_ids or []
        base = base.where(Doctor.user_id.in_(ids) if online else Doctor.user_id.not_in(ids))

    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0

    # Orden estable para paginar (nombre + id como desempate); el "online" lo ordena el cliente.
    page = base.order_by(Doctor.full_name, Doctor.id).offset(skip).limit(limit)
    rows = (await session.execute(page)).all()
    items = [
        {
            "id": r.id,
            "user_id": r.user_id,
            "full_name": r.full_name,
            "specialty_id": r.specialty_id,
            "professional_type_id": r.professional_type_id,
        }
        for r in rows
    ]
    return items, total


async def reveal_doctor_contact(
    session: AsyncSession, doctor_id: uuid.UUID, viewer_user_id: uuid.UUID
) -> str | None:
    """Devuelve el teléfono de contacto de un médico del pool y REGISTRA en audit_log quién lo
    vio (para la bitácora del panel admin). El número no se expone en el listado del pool: solo
    aquí, ligado a un evento de auditoría."""
    doctor = await get_doctor(session, doctor_id)  # 404 si no existe/está borrado
    phone = doctor.phone
    if phone is None and doctor.user_id is not None:
        user = await session.get(Profile, doctor.user_id)
        phone = user.whatsapp_number if user else None
    await audit.log_action(
        session,
        action="doctor.contact_viewed",
        actor_user_id=viewer_user_id,
        resource="doctors",
        resource_id=doctor_id,
        metadata={"doctor_name": doctor.full_name},
    )
    await session.commit()
    return phone


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


async def _professional_type_name(
    session: AsyncSession, professional_type_id: uuid.UUID | None
) -> str | None:
    if professional_type_id is None:
        return None
    return await session.scalar(
        select(ProfessionalType.name).where(ProfessionalType.id == professional_type_id)
    )


async def _assert_cedula_available(
    session: AsyncSession, cedula: str, *, exclude_doctor_id: uuid.UUID | None = None
) -> None:
    """La cédula no puede pertenecer a otra ficha activa (mismo criterio que el índice
    único parcial `uq_doctors_cedula_not_deleted`). Se comprueba antes de escribir para
    devolver un 409 con mensaje de dominio en vez del error de integridad genérico."""
    stmt = select(Doctor.id).where(Doctor.cedula == cedula, Doctor.deleted_at.is_(None))
    if exclude_doctor_id is not None:
        stmt = stmt.where(Doctor.id != exclude_doctor_id)
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        raise ConflictError("La cédula ya pertenece a otro médico.")


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


async def _me_from_doctor_row(
    session: AsyncSession, user_id: uuid.UUID, doctor: Doctor
) -> DoctorMeResponse:
    """Perfil propio a partir de la ficha `doctors`, resolviendo los nombres de especialidad
    y tipo profesional."""
    return DoctorMeResponse(
        source="doctor",
        user_id=user_id,
        doctor_id=doctor.id,
        cedula=doctor.cedula,
        full_name=doctor.full_name,
        license=doctor.license,
        specialty_id=doctor.specialty_id,
        specialty=await _specialty_name(session, doctor.specialty_id),
        professional_type_id=doctor.professional_type_id,
        professional_type=await _professional_type_name(session, doctor.professional_type_id),
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
        return await _me_from_doctor_row(session, user_id, doctor)
    return _me_from_profile(await _my_doctor_profile(session, user_id))


async def _update_my_doctor_row(
    session: AsyncSession, user_id: uuid.UUID, doctor: Doctor, data: DoctorSelfUpdate
) -> DoctorMeResponse:
    fields = data.model_dump(exclude_unset=True)
    # El tipo profesional no es auto-editable en una ficha existente (solo se usa al
    # crearla desde una cuenta sin ficha); se ignora si viene en el payload.
    fields.pop("professional_type_id", None)
    new_cedula = fields.pop("cedula", None)
    for field, value in fields.items():
        setattr(doctor, field, value)
    # Cambiar la cédula re-verifica contra el registro oficial de su tipo y
    # recalcula `verified` (fail-closed si ya no valida).
    if new_cedula is not None and new_cedula != doctor.cedula:
        await _assert_cedula_available(session, new_cedula, exclude_doctor_id=doctor.id)
        doctor.cedula = new_cedula
        doctor.verified = await _verify_credential(
            session, doctor.professional_type_id, new_cedula
        )
    await _sync_user_from_doctor(session, doctor)
    await session.commit()
    await session.refresh(doctor)
    return await _me_from_doctor_row(session, user_id, doctor)


async def _complete_registration_from_user(
    session: AsyncSession, profile: Profile, data: DoctorSelfUpdate
) -> DoctorMeResponse:
    """Una cuenta sin ficha (`source:"user"`, médico de Google) completa su registro:
    verifica la cédula contra el registro oficial de su tipo (SACS/FPV) y **crea** la
    fila en `doctors`, promoviéndola a `source:"doctor"`.

    `professional_type_id` es obligatorio (elige el registro); sin él no se puede
    verificar (422). `verified` refleja el resultado del registro (True si la cédula es
    válida, False si no se encuentra o el servicio falla) — igual que el alta pública,
    la ficha se crea de todos modos y el frontend muestra el estado por `verified`."""
    fields = data.model_dump(exclude_unset=True)
    professional_type_id = fields.get("professional_type_id")
    if professional_type_id is None:
        raise UnprocessableError("Indica el tipo de profesional para verificar tu cédula.")
    cedula = fields["cedula"]  # el caller garantiza que viene
    await _assert_cedula_available(session, cedula)
    verified = await _verify_credential(session, professional_type_id, cedula)
    doctor = Doctor(
        user_id=profile.id,
        professional_type_id=professional_type_id,
        specialty_id=fields.get("specialty_id"),
        cedula=cedula,
        full_name=fields.get("full_name") or profile.full_name,
        license=fields.get("license", profile.medical_license),
        phone=profile.whatsapp_number,
        email=profile.email,
        country_of_residence=profile.country,
        verified=verified,
    )
    session.add(doctor)
    await session.flush()
    await _sync_user_from_doctor(session, doctor)
    await session.commit()
    await session.refresh(doctor)
    return await _me_from_doctor_row(session, profile.id, doctor)


async def _update_my_profile_row(
    session: AsyncSession, user_id: uuid.UUID, data: DoctorSelfUpdate
) -> DoctorMeResponse:
    profile = await _my_doctor_profile(session, user_id)
    fields = data.model_dump(exclude_unset=True)
    # Completar/verificar la cédula = crear la ficha en `doctors` (promoción a source:"doctor").
    if fields.get("cedula") is not None:
        return await _complete_registration_from_user(session, profile, data)
    # Sin cédula: solo edición de los campos que viven en `users` (professional_type_id,
    # que users no almacena, se ignora aquí).
    if fields.get("full_name") is not None:
        profile.full_name = fields["full_name"]
    if "license" in fields:
        profile.medical_license = fields["license"]
    if "specialty_id" in fields:
        profile.specialty_id = fields["specialty_id"]
        profile.specialty = await _specialty_name(session, fields["specialty_id"])
    await session.commit()
    await session.refresh(profile)
    return _me_from_profile(profile)


async def update_my_profile(
    session: AsyncSession, user_id: uuid.UUID, data: DoctorSelfUpdate
) -> DoctorMeResponse:
    """Auto-edición del perfil propio. Sobre la fila `doctors` cambiar la cédula
    re-verifica SACS/FPV; una cuenta sin ficha que envía `cedula` + `professional_type_id`
    la verifica y crea su ficha (promoción a `source:"doctor"`)."""
    doctor = await _my_doctor_row(session, user_id)
    if doctor is not None:
        return await _update_my_doctor_row(session, user_id, doctor, data)
    return await _update_my_profile_row(session, user_id, data)
