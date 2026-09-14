"""La RLS de Supabase dice lo mismo que la API (migración 20260914_111456).

La API se conecta como dueña de la base y se salta la RLS, así que su suite no ve lo que puede
hacer un navegador con el anon key y un JWT propio. Estas pruebas se hacen pasar por ese
navegador: `set local role authenticated` + los claims del JWT, contra el Supabase local real.

Lo que fijan:
- una cuenta con rol médico pero sin ficha habilitada no es staff para la base (antes leía todos
  los pacientes por PostgREST aunque la API ya la bloqueara);
- nadie lee `patients` ni `consultation_events` directo; de `consultations`, solo las columnas
  de la señal de Realtime;
- el paciente con cuenta sigue viendo sus consultas;
- el criterio SQL de "puede atender" es el mismo que el de la API.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.rbac import Role, UserRole
from src.services import doctors as doctors_service
from tests._helpers import add_doctor, any_specialty_id, make_profile

PREFIX = "/api/v1"


async def _as_browser(session: AsyncSession, user_id: uuid.UUID) -> None:
    """A partir de aquí, lo que ve PostgREST para ese usuario. `set local`: se deshace con el
    rollback del test. Siembra ANTES de llamar a esto: `authenticated` no puede insertar."""
    claims = json.dumps({"sub": str(user_id), "role": "authenticated"})
    await session.execute(text("select set_config('request.jwt.claims', :c, true)"), {"c": claims})
    await session.execute(text("set local role authenticated"))


async def _role(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    await _as_browser(session, user_id)
    role = await session.scalar(text("select public.current_user_role()"))
    await session.execute(text("reset role"))
    return role


async def _patient_with_consultation(
    client: AsyncClient, user_id: uuid.UUID | None = None
) -> tuple[str, str]:
    body = {
        "full_name": "Paciente RLS",
        "phone_whatsapp": "+58412555000",
        "affected_zone": "Caracas",
        "cedula": "V-11222333",
        "consent": True,
    }
    if user_id is not None:
        body["user_id"] = str(user_id)
    patient = await client.post(f"{PREFIX}/patients", json=body)
    assert patient.status_code == 201, patient.text
    consultation = await client.post(
        f"{PREFIX}/consultations",
        json={
            "patient_id": patient.json()["id"],
            "specialty_id": await any_specialty_id(client),
            "chief_complaint": "Dolor de cabeza",
        },
    )
    assert consultation.status_code == 201, consultation.text
    return patient.json()["id"], consultation.json()["id"]


async def test_doctor_can_practice_igual_que_la_api(db_session: AsyncSession) -> None:
    """`public.doctor_can_practice` (RLS) y `has_valid_credential` (API) son el MISMO criterio.
    Si divergen, la base deja leer a quien la API bloquea (el bug que cerró esta migración) o
    al revés. Cada variante rompe exactamente un requisito."""
    variants = {
        "habilitado": {},
        "no_verificado": {"verified": False},
        "de_baja": {"status": 0},
        "expulsado": {"status": 2},
        # (Una cédula en blanco no llega a existir: la frena el CHECK `doctors_cedula_format`.)
        "sin_cedula": {"cedula": None},
        "sin_licencia": {"license": None},
        "licencia_en_blanco": {"license": " "},
        "borrado": {"deleted_at": datetime.now(UTC)},
    }
    users = {name: await add_doctor(db_session, **fields) for name, fields in variants.items()}
    sin_ficha = make_profile(role="doctor")
    db_session.add(sin_ficha)
    await db_session.flush()
    users["sin_ficha"] = sin_ficha

    for name, user in users.items():
        api = await doctors_service.has_valid_credential(db_session, user.id)
        sql = await db_session.scalar(
            text("select public.doctor_can_practice(:uid)"), {"uid": user.id}
        )
        assert api is sql, f"{name}: API={api} SQL={sql}"
    assert await doctors_service.has_valid_credential(db_session, users["habilitado"].id)


async def test_current_user_role_exige_ficha_habilitada_al_medico(
    db_session: AsyncSession,
) -> None:
    habilitado = await add_doctor(db_session)
    no_verificado = await add_doctor(db_session, verified=False)
    sin_ficha = make_profile(role="doctor")
    paciente = make_profile(role="patient")
    admin = make_profile(role="admin")
    # Rol legado 'doctor' + super_admin por RBAC, sin ficha: como en la API, un admin no
    # depende de su ficha (hay cuentas así en producción).
    admin_dual = make_profile(role="doctor")
    db_session.add_all([sin_ficha, paciente, admin, admin_dual])
    await db_session.flush()
    super_admin_id = await db_session.scalar(select(Role.id).where(Role.code == "super_admin"))
    db_session.add(UserRole(user_id=admin_dual.id, role_id=super_admin_id))
    await db_session.flush()

    assert await _role(db_session, habilitado.id) == "doctor"
    assert await _role(db_session, no_verificado.id) is None
    assert await _role(db_session, sin_ficha.id) is None
    assert await _role(db_session, paciente.id) == "patient"
    assert await _role(db_session, admin.id) == "admin"
    assert await _role(db_session, admin_dual.id) == "doctor"


async def test_medico_habilitado_no_lee_pacientes_ni_detalle_de_consultas_directo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Sus pacientes los ve por la API (que valida pertenencia); por PostgREST, nada de
    `patients` y de `consultations` solo la señal que usa Realtime."""
    _, consultation_id = await _patient_with_consultation(client)
    doctor = await add_doctor(db_session)
    await _as_browser(db_session, doctor.id)

    # Primero lo que SÍ puede: la señal de Realtime (antes de cualquier error, que aborta la
    # transacción hasta el savepoint).
    row = (
        await db_session.execute(
            text("select id, status, assigned_doctor_id from public.consultations where id = :id"),
            {"id": consultation_id},
        )
    ).one()
    assert str(row.id) == consultation_id and row.status == "waiting"

    denied = [
        "select count(*) from public.patients",
        "select count(*) from public.consultation_events",
        *(
            f"select {column} from public.consultations"
            for column in ("chief_complaint", "video_room_url", "internal_note", "patient_id")
        ),
    ]
    for sql in denied:
        await _assert_permission_denied(db_session, sql)


async def test_cuenta_de_medico_sin_ficha_no_ve_ninguna_consulta(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, consultation_id = await _patient_with_consultation(client)
    sin_ficha = make_profile(role="doctor")
    db_session.add(sin_ficha)
    await db_session.flush()
    await _as_browser(db_session, sin_ficha.id)

    visible = await db_session.scalar(
        text("select count(*) from public.consultations where id = :id"), {"id": consultation_id}
    )
    assert visible == 0


async def test_paciente_con_cuenta_sigue_viendo_lo_suyo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = make_profile(role="patient")
    db_session.add(owner)
    await db_session.flush()
    _, own_consultation = await _patient_with_consultation(client, owner.id)
    _, other_consultation = await _patient_with_consultation(client)
    await _as_browser(db_session, owner.id)

    # Sus consultas sí (la policy ya no necesita leer `patients` con sus privilegios)...
    consultations = set(
        (await db_session.scalars(text("select id::text from public.consultations"))).all()
    )
    assert own_consultation in consultations and other_consultation not in consultations
    # ...la tabla `patients`, por PostgREST, tampoco: sus datos los ve por la API (/mi-caso).
    await _assert_permission_denied(db_session, "select count(*) from public.patients")


async def test_helpers_de_rls_no_revelan_nada_ajeno_por_rpc(db_session: AsyncSession) -> None:
    """`doctor_can_practice` invocable por `authenticated` dejaría preguntar por /rpc si un
    user_id ajeno está habilitado: solo la usan las funciones de RLS (security definer).
    `owns_patient` sí es ejecutable —la evalúa la policy con los privilegios de quien consulta—,
    pero solo responde por pacientes propios."""
    doctor = await add_doctor(db_session)
    await _as_browser(db_session, doctor.id)
    assert (
        await db_session.scalar(text("select public.owns_patient(:p)"), {"p": uuid.uuid4()})
        is False
    )
    await _assert_permission_denied(
        db_session, "select public.doctor_can_practice(:uid)", {"uid": doctor.id}
    )


async def _assert_permission_denied(
    session: AsyncSession, sql: str, params: dict | None = None
) -> None:
    """El error aborta la transacción: se aísla en un savepoint para poder seguir probando."""
    with pytest.raises(DBAPIError, match="permission denied"):
        async with session.begin_nested():
            await session.execute(text(sql), params or {})
