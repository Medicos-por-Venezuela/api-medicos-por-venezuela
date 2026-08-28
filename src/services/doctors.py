"""Capa de negocio para doctors.

Al registrar, el backend **verifica la credencial** contra el registro oficial que
corresponde al tipo profesional elegido: Médico -> SACS, Psicólogo -> FPV. `verified`
queda en True solo si ese registro encuentra la cédula **y devuelve nombre y licencia**;
en cualquier otro caso (tipo desconocido, servicio caído, no encontrado, respuesta sin
nombre o sin licencia) queda en False (fail-closed) y la habilita un admin a mano
(`approve_doctor`).

⚠️ El nombre y la licencia que manda el cliente **no verifican nada**: son los datos que
él mismo escribió. Cuando el registro oficial responde, sus valores son los que se
escriben en la ficha (ver `_apply_official_identity`).
"""

import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import NamedTuple

from sqlalchemy import (
    Integer,
    Select,
    String,
    case,
    cast,
    exists,
    false,
    func,
    literal,
    null,
    or_,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

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


class CredentialCheck(NamedTuple):
    """Lo que el registro oficial (SACS/FPV) dice de una cédula.

    `verified` es True **solo** si el registro respondió, encontró la cédula y devolvió
    nombre y licencia. En ese caso `full_name`/`license` traen los valores OFICIALES —
    los únicos con los que se puede rellenar la ficha, porque son los únicos que
    verifican algo. Si falta cualquiera de los dos, la credencial no está verificada:
    una ficha sin licencia no habilita a nadie (`has_valid_credential` la bloquea), así
    que darla por buena solo produciría un verificado que no puede atender.
    """

    verified: bool
    full_name: str | None = None
    license: str | None = None


# Resultado único para todos los caminos de "el registro no valida esta cédula".
_UNVERIFIED = CredentialCheck(verified=False)


def _official_identity(
    nombre: str | None, apellido: str | None, licencia: str | None
) -> CredentialCheck:
    """Construye el resultado a partir de los campos que devuelve un registro oficial.

    Exige nombre Y licencia no vacíos (ambos registros los devuelven cuando el
    profesional existe de verdad); el apellido es opcional porque no todos los
    registros lo traen completo.
    """
    full_name = " ".join(part.strip() for part in (nombre or "", apellido or "") if part.strip())
    license_ = (licencia or "").strip()
    if not full_name or not license_:
        return _UNVERIFIED
    return CredentialCheck(verified=True, full_name=full_name, license=license_)


async def _check_in_sacs(cedula: str) -> CredentialCheck:
    """El SACS confirma que la cédula corresponde a un médico registrado."""
    result = await sacs_service.verificar_sacs(cedula)
    if not (result.encontrado and result.es_medico):
        return _UNVERIFIED
    return _official_identity(result.nombre, result.apellido, result.licencia)


async def _check_in_fpv(cedula: str) -> CredentialCheck:
    """La FPV confirma que la cédula corresponde a un psicólogo colegiado."""
    result = await psicologo_service.verificar_psicologo(cedula)
    if not result.encontrado:
        return _UNVERIFIED
    return _official_identity(result.nombre, result.apellido, result.licencia)


# Tipo profesional (normalizado, sin acentos) -> registro oficial que lo valida.
# Añadir un tipo verificable = una entrada más, sin tocar la lógica de ruteo.
_CREDENTIAL_VERIFIERS: dict[str, Callable[[str], Awaitable[CredentialCheck]]] = {
    "medico": _check_in_sacs,
    "psicologo": _check_in_fpv,
}


async def _verify_credential(
    session: AsyncSession, professional_type_id: uuid.UUID | None, cedula: str
) -> CredentialCheck:
    """Consulta el registro oficial del tipo profesional y devuelve su veredicto.

    Fail-closed: sin tipo, tipo inexistente o tipo sin registro verificable
    (p. ej. nutricionista) -> no verificado.
    """
    if professional_type_id is None:
        return _UNVERIFIED
    ptype = await session.get(ProfessionalType, professional_type_id)
    if ptype is None:
        return _UNVERIFIED
    verify = _CREDENTIAL_VERIFIERS.get(_normalize(ptype.name))
    return await verify(cedula) if verify else _UNVERIFIED


def _apply_official_identity(doctor: Doctor, check: CredentialCheck) -> None:
    """Sobrescribe nombre y licencia de la ficha con los del registro oficial.

    **Solo cuando la verificación tuvo éxito.** Decisión explícita: si el registro no
    respondió, no encontró la cédula o no trajo nombre/licencia, no hay valores oficiales
    que copiar, y borrar lo que escribió el médico dejaría la ficha vacía justo cuando lo
    que hace falta es que un humano la revise. Esos datos del cliente NO habilitan nada
    (la ficha queda `verified=false` y el gate la bloquea): son una declaración pendiente
    de aprobación, no una credencial.
    """
    if not check.verified:
        return
    doctor.full_name = check.full_name
    doctor.license = check.license


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


# --- Habilitación para atender: una sola definición ---------------------------
# El gate (`has_valid_credential`) y el listado admin (`list_doctors`) tienen que decir
# exactamente lo mismo: si divergen, el admin ve "aprobado" a quien el backend bloquea.
# Por eso ambos salen de `_blocked_reason`.

SIN_FICHA = "sin_ficha"
DE_BAJA = "de_baja"
SIN_CEDULA = "sin_cedula"
SIN_LICENCIA = "sin_licencia"
NO_VERIFICADO = "no_verificado"


def _blank(column):
    """SQL: la columna es NULL o solo espacios (un dato que no sustenta credencial alguna)."""
    return func.coalesce(func.btrim(column), "") == ""


def _blocked_reason(has_record, status, verified, cedula, license_):
    """Motivo por el que la ficha NO habilita a atender, o NULL si sí habilita.

    Es la ÚNICA definición del criterio en el backend. El orden importa: se reporta el
    primer motivo que aplica, y es el que le dice al admin qué hacer.

    `no_verificado` va deliberadamente al final: cuando ese es el motivo, aprobar a mano
    SÍ habilita al médico. En los otros cuatro, marcar `verified` no cambiaría nada — hay
    que pedirle la cédula/licencia o reactivar la ficha primero.
    """
    return case(
        (has_record.is_(False), literal(SIN_FICHA)),
        (status != 1, literal(DE_BAJA)),
        (_blank(cedula), literal(SIN_CEDULA)),
        (_blank(license_), literal(SIN_LICENCIA)),
        (verified.is_(False), literal(NO_VERIFICADO)),
        else_=null(),
    )


def _doctor_blocked_reason():
    """`_blocked_reason` aplicado a las columnas de una fila `doctors` existente."""
    return _blocked_reason(true(), Doctor.status, Doctor.verified, Doctor.cedula, Doctor.license)


async def has_valid_credential(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """El médico está habilitado para atender: tiene ficha activa en `doctors` con la
    credencial verificada Y los datos que la sustentan (cédula + licencia).

    Es el gate de acceso de los médicos (lo consulta `get_current_principal`): sin esto
    la cuenta conserva su rol pero pierde TODOS los permisos, igual que una revocada.
    Cubre los casos de "no debería estar atendiendo" que enumera `_blocked_reason`:
      - sin ficha (cuenta de Google que nunca completó su registro),
      - ficha con `verified=false` (el SACS/FPV la rechazó o no respondió — fail-closed),
      - ficha marcada verificada pero sin cédula o sin licencia (backfill legacy),
      - ficha dada de baja (status 0) o expulsada (2).

    Se vuelve a `true` cuando el SACS/FPV valida la cédula (al registrarse o al corregirla
    en `PATCH /doctors/me`) o cuando un admin aprueba la ficha a mano
    (`POST /doctors/{id}/approve`, que queda en `audit_log`).
    """
    stmt = (
        select(Doctor.id)
        .where(
            Doctor.user_id == user_id,
            Doctor.deleted_at.is_(None),
            _doctor_blocked_reason().is_(None),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


def _fichas_select() -> Select:
    """Rama del listado admin con las fichas de `doctors` vivas (LEFT JOIN a su cuenta)."""
    return (
        select(
            Doctor.id.label("id"),
            Doctor.id.label("row_key"),
            Doctor.user_id.label("user_id"),
            Doctor.full_name.label("full_name"),
            Doctor.cedula.label("cedula"),
            Doctor.license.label("license"),
            func.coalesce(Doctor.email, Profile.email).label("email"),
            Doctor.specialty_id.label("specialty_id"),
            Doctor.professional_type_id.label("professional_type_id"),
            cast(Doctor.status, Integer).label("status"),
            Doctor.verified.label("verified"),
            Doctor.created_at.label("created_at"),
            true().label("has_record"),
        )
        .select_from(Doctor)
        .outerjoin(Profile, Profile.id == Doctor.user_id)
        .where(Doctor.deleted_at.is_(None))
    )


def _sin_ficha_select() -> Select:
    """Rama del listado admin con las cuentas de médico que NUNCA crearon ficha.

    Sin esto el panel no las vería (no existen en `doctors`) y serían justo las que hay
    que perseguir: no se las puede aprobar, tienen que capturar su cédula ellas mismas.
    """
    return select(
        cast(null(), UUID(as_uuid=True)).label("id"),
        Profile.id.label("row_key"),
        Profile.id.label("user_id"),
        Profile.full_name.label("full_name"),
        cast(null(), String).label("cedula"),
        Profile.medical_license.label("license"),
        Profile.email.label("email"),
        cast(null(), UUID(as_uuid=True)).label("specialty_id"),
        cast(null(), UUID(as_uuid=True)).label("professional_type_id"),
        cast(null(), Integer).label("status"),
        false().label("verified"),
        Profile.created_at.label("created_at"),
        false().label("has_record"),
    ).where(
        Profile.role.in_(_DOCTOR_PROFILE_ROLES),
        ~exists(
            select(Doctor.id).where(Doctor.user_id == Profile.id, Doctor.deleted_at.is_(None))
        ),
    )


async def list_doctors(
    session: AsyncSession,
    *,
    skip: int = 0,
    limit: int = 100,
    status: int | None = None,
    verified: bool | None = None,
    can_practice: bool | None = None,
    blocked_reason: str | None = None,
    search: str | None = None,
) -> tuple[list[dict], int]:
    """Médicos para la tabla del panel admin: filas + total exacto (para paginar ~3000).

    El universo son las fichas vivas de `doctors` MÁS las cuentas con rol de médico que
    todavía no tienen ficha: las dos poblaciones que el admin necesita perseguir.

    Cada fila trae `blocked_reason`/`can_practice` (de `_blocked_reason`, el mismo criterio
    que el gate), así que el admin ve de un vistazo a quién puede aprobar (`no_verificado`)
    y a quién hay que pedirle datos.

    Filtros: `status`, `verified`, `can_practice` ("habilitado para atender", que NO es lo
    mismo que `verified`), `blocked_reason` y `search` por nombre/cédula/email.

    **`blocked_reason` es el filtro operativo de verdad**, no un lujo: en los datos reales
    el 95% de los bloqueados es `sin_cedula`, así que los que un admin SÍ puede aprobar
    (`no_verificado`) quedan sepultados y no aparecen ni en la primera página. Sin este
    filtro el panel enseña un montón de filas sin botón y parece que aprobar no existe.
    """
    sub = _fichas_select().union_all(_sin_ficha_select()).subquery()
    blocked = _blocked_reason(
        sub.c.has_record, sub.c.status, sub.c.verified, sub.c.cedula, sub.c.license
    ).label("blocked_reason")

    base = select(sub, blocked)
    if status is not None:
        base = base.where(sub.c.status == status)
    if verified is not None:
        base = base.where(sub.c.verified.is_(verified))
    if can_practice is not None:
        criterion = blocked.is_(None) if can_practice else blocked.is_not(None)
        base = base.where(criterion)
    if blocked_reason is not None:
        base = base.where(blocked == blocked_reason)
    if search and (term := search.strip()):
        like = f"%{term}%"
        base = base.where(
            or_(
                sub.c.full_name.ilike(like),
                sub.c.cedula.ilike(like),
                sub.c.email.ilike(like),
            )
        )

    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    # Desempate por `row_key` (id de la ficha, o de la cuenta si no la tiene): sin él la
    # paginación puede repetir u omitir filas con el mismo `created_at`.
    page = base.order_by(sub.c.created_at.desc(), sub.c.row_key).offset(skip).limit(limit)
    rows = (await session.execute(page)).all()
    # `row_key`/`has_record` son plomería de la unión y no salen en la respuesta.
    items = [
        {
            "id": r.id,
            "user_id": r.user_id,
            "full_name": r.full_name,
            "cedula": r.cedula,
            "license": r.license,
            "email": r.email,
            "specialty_id": r.specialty_id,
            "professional_type_id": r.professional_type_id,
            "status": r.status,
            "verified": r.verified,
            "created_at": r.created_at,
            "can_practice": r.blocked_reason is None,
            "blocked_reason": r.blocked_reason,
        }
        for r in rows
    ]
    return items, total


async def credential_summary(session: AsyncSession) -> dict[str, int]:
    """Cuántos médicos hay en cada estado de credencial, en UNA sola pasada.

    Es lo que le dice al admin qué tiene delante al abrir el panel: cuántos atienden hoy y,
    de los bloqueados, cuántos puede desbloquear él (`no_verificado`) frente a cuántos
    dependen de que el médico complete su ficha. Sin este resumen la tabla ordenada por
    fecha esconde a los aprobables entre miles de fichas sin cédula y el panel aparenta no
    tener nada que aprobar.
    """
    sub = _fichas_select().union_all(_sin_ficha_select()).subquery()
    blocked = _blocked_reason(
        sub.c.has_record, sub.c.status, sub.c.verified, sub.c.cedula, sub.c.license
    ).label("blocked_reason")
    rows = (await session.execute(select(blocked, func.count()).group_by(blocked))).all()
    # Todas las claves siempre presentes (0 si el grupo está vacío): el frontend pinta la
    # misma fila de contadores sin comprobar cada una.
    counts = dict.fromkeys(
        ("can_practice", SIN_FICHA, DE_BAJA, SIN_CEDULA, SIN_LICENCIA, NO_VERIFICADO), 0
    )
    for reason, count in rows:
        counts["can_practice" if reason is None else reason] = count
    counts["total"] = sum(counts.values())
    return counts


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
    """Registra un médico. `verified` se decide contra SACS/FPV; `status` = 1 (activo).

    Si el registro oficial valida la cédula, el nombre y la licencia de la ficha son los
    SUYOS, no los del payload (ver `_apply_official_identity`)."""
    # Honeypot: si el campo trampa llegó con valor, es un bot. Rechazo genérico.
    if data.website:
        raise BadRequestError("Solicitud inválida.")
    check = await _verify_credential(session, data.professional_type_id, data.cedula)
    # Liga el doctor a su cuenta (users) por email, si ya existe. El signup crea la cuenta
    # justo antes de este POST, así que normalmente la resuelve. Server-side (no lo manda el
    # cliente) para evitar IDOR.
    user_id = (
        await session.execute(select(Profile.id).where(Profile.email == data.email))
    ).scalar_one_or_none()
    doctor = Doctor(
        **data.model_dump(exclude={"website"}), verified=check.verified, user_id=user_id
    )
    _apply_official_identity(doctor, check)
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


# Motivos de bloqueo que la aprobación manual NO arregla -> qué decirle al admin.
# `no_verificado` (y "ninguno") faltan a propósito: son justo los aprobables.
_NOT_APPROVABLE = {
    DE_BAJA: "está dada de baja o expulsada",
    SIN_CEDULA: "le falta la cédula",
    SIN_LICENCIA: "le falta el número de licencia",
}


async def _assert_approvable(session: AsyncSession, doctor: Doctor) -> None:
    """La aprobación manual solo tiene sentido si es lo ÚNICO que le falta a la ficha.

    Aprobar una ficha incompleta no habilita a nadie —`has_valid_credential` la sigue
    bloqueando— pero deja al admin creyendo que sí: es el caso de la inmensa mayoría de
    los médicos bloqueados hoy (verificados legacy sin cédula). Mejor un 422 que diga qué
    falta que un 200 que no sirve para nada.

    El motivo se calcula con la MISMA expresión SQL que publica el listado admin, así que
    el invariante se sostiene solo: `blocked_reason == "no_verificado"` <=> aprobar funciona.
    """
    reason = await session.scalar(select(_doctor_blocked_reason()).where(Doctor.id == doctor.id))
    if problem := _NOT_APPROVABLE.get(reason):
        raise UnprocessableError(
            f"No se puede aprobar esta ficha: {problem}. Aprobarla no habilitaría al médico "
            "(atender exige ficha activa con cédula y licencia); resuélvelo primero."
        )


async def approve_doctor(
    session: AsyncSession, doctor_id: uuid.UUID, actor_user_id: uuid.UUID | None = None
) -> Doctor:
    """Aprobación manual: un admin habilita a un médico al que el registro oficial no validó.

    Deja su propia acción en `audit_log` (`doctor.approved`, en la misma transacción). No es
    un `doctor.updated` cualquiera: es la traza de que un humano —y cuál— decidió dejar
    atender a alguien que el SACS/FPV no respaldó. Idempotente: aprobar dos veces no falla,
    y la segunda también queda registrada (la intención se auditó igual).
    """
    doctor = await get_doctor(session, doctor_id)
    await _assert_approvable(session, doctor)
    was_verified = doctor.verified
    doctor.verified = True
    await audit.log_action(
        session,
        action="doctor.approved",
        actor_user_id=actor_user_id,
        resource="doctors",
        resource_id=doctor.id,
        metadata={"doctor_name": doctor.full_name, "was_verified": was_verified},
    )
    await session.commit()
    await session.refresh(doctor)
    return doctor


async def revoke_doctor_approval(
    session: AsyncSession, doctor_id: uuid.UUID, actor_user_id: uuid.UUID | None = None
) -> Doctor:
    """Deshace la aprobación: `verified` vuelve a false y el médico deja de poder atender.

    El reverso de `approve_doctor` (aprobación por error, credencial impugnada). No exige
    ficha completa: retirar el permiso siempre es válido. Queda como `doctor.approval_revoked`.
    """
    doctor = await get_doctor(session, doctor_id)
    was_verified = doctor.verified
    doctor.verified = False
    await audit.log_action(
        session,
        action="doctor.approval_revoked",
        actor_user_id=actor_user_id,
        resource="doctors",
        resource_id=doctor.id,
        metadata={"doctor_name": doctor.full_name, "was_verified": was_verified},
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
    # recalcula `verified` (fail-closed si ya no valida). Si valida, el nombre y la
    # licencia pasan a ser los del registro y pisan lo que venga en este mismo payload.
    if new_cedula is not None and new_cedula != doctor.cedula:
        await _assert_cedula_available(session, new_cedula, exclude_doctor_id=doctor.id)
        doctor.cedula = new_cedula
        check = await _verify_credential(session, doctor.professional_type_id, new_cedula)
        doctor.verified = check.verified
        _apply_official_identity(doctor, check)
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
    válida y trae nombre y licencia, False si no se encuentra o el servicio falla) — igual
    que el alta pública, la ficha se crea de todos modos, con el nombre y la licencia del
    registro si verificó, y el frontend muestra el estado por `verified`."""
    fields = data.model_dump(exclude_unset=True)
    professional_type_id = fields.get("professional_type_id")
    if professional_type_id is None:
        raise UnprocessableError("Indica el tipo de profesional para verificar tu cédula.")
    cedula = fields["cedula"]  # el caller garantiza que viene
    await _assert_cedula_available(session, cedula)
    check = await _verify_credential(session, professional_type_id, cedula)
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
        verified=check.verified,
    )
    _apply_official_identity(doctor, check)
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
