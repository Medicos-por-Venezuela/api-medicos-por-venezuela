"""El trigger crea una fila en doctors cuando un usuario es/pasa a role='doctor'."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.doctor import Doctor
from src.models.professional_type import ProfessionalType
from tests._helpers import make_profile


async def _doctor_of(db_session: AsyncSession, user_id) -> Doctor | None:
    return (
        await db_session.execute(
            select(Doctor).where(Doctor.user_id == user_id, Doctor.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def _ptype_name(db_session: AsyncSession, ptype_id) -> str:
    return (
        await db_session.execute(
            select(ProfessionalType.name).where(ProfessionalType.id == ptype_id)
        )
    ).scalar_one()


async def test_nuevo_doctor_crea_fila_en_doctors(db_session: AsyncSession) -> None:
    prof = make_profile(role="doctor", specialty="Cardiología")
    prof.whatsapp_number = "+584140000000"
    db_session.add(prof)
    await db_session.flush()  # dispara el trigger AFTER INSERT

    doc = await _doctor_of(db_session, prof.id)
    assert doc is not None
    assert doc.user_id == prof.id
    assert doc.cedula is None  # cédula null (se completa por el frontend)
    assert doc.phone == "+584140000000"  # whatsapp copiado tal cual
    assert await _ptype_name(db_session, doc.professional_type_id) == "Médico"


async def test_psicologia_mapea_a_psicologo(db_session: AsyncSession) -> None:
    prof = make_profile(role="doctor", specialty="Psicología")
    db_session.add(prof)
    await db_session.flush()

    doc = await _doctor_of(db_session, prof.id)
    assert doc is not None
    assert await _ptype_name(db_session, doc.professional_type_id) == "Psicólogo"


async def test_paciente_no_crea_doctor(db_session: AsyncSession) -> None:
    prof = make_profile(role="patient")
    db_session.add(prof)
    await db_session.flush()

    assert await _doctor_of(db_session, prof.id) is None


async def test_finalizar_rol_a_doctor_crea_doctor(db_session: AsyncSession) -> None:
    """UPDATE role patient->doctor (flujo set_my_role) también crea el doctor."""
    prof = make_profile(role="patient")
    db_session.add(prof)
    await db_session.flush()
    assert await _doctor_of(db_session, prof.id) is None

    prof.role = "doctor"
    prof.specialty = "Neurología"
    await db_session.flush()  # UPDATE OF role/specialty -> dispara el trigger

    doc = await _doctor_of(db_session, prof.id)
    assert doc is not None
    assert await _ptype_name(db_session, doc.professional_type_id) == "Médico"
