"""Cifrado de campos clínicos: el módulo, el tipo de columna, los esquemas y el backfill.

Lo que fijan:
- AES-256-GCM con AAD por columna: un texto cifrado movido a otra columna no descifra (sin eso,
  quien tenga la base podría copiar la nota interna al motivo, que sí ve el paciente);
- rotación: se descifra con cualquier clave del llavero y se cifra con la activa;
- `Sealed` nunca se convierte solo a texto (logs, f-strings, correos dan el marcador);
- la base guarda `enc:v1:`, el ORM devuelve `Sealed`, y los esquemas solo descifran con permiso;
- el script de backfill cifra lo legado y re-cifra lo rotado sin tocar lo que ya está al día.
"""

import base64
import logging
import os
import uuid

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from scripts import encrypt_clinical_data as backfill
from src.core import clinical_crypto
from src.core.clinical_crypto import (
    REDACTED,
    ClinicalCryptoError,
    Keyring,
    Sealed,
    generate_key,
    reveal,
)
from src.models.patient import Patient
from src.models.profile import Profile
from src.schemas.clinical import (
    ClinicalAccessMixin,
    ClinicalNote,
    ClinicalSummary,
    clinical_context,
    summary_grant,
    treating_grant,
)

FIELD = "consultations.chief_complaint"


@pytest.fixture
def ring() -> Keyring:
    return Keyring.from_config(generate_key())


# --- Módulo de cifrado ---


def test_roundtrip_y_nonce_distinto_cada_vez(ring: Keyring) -> None:
    a = ring.encrypt("dolor torácico", field=FIELD)
    b = ring.encrypt("dolor torácico", field=FIELD)

    assert a.startswith(f"enc:v1:{ring.active_kid}:")
    assert a != b  # nonce aleatorio: el mismo motivo no produce el mismo texto cifrado
    assert "dolor" not in a
    assert ring.decrypt(a, field=FIELD) == "dolor torácico"


def test_texto_cifrado_de_otra_columna_no_descifra(ring: Keyring) -> None:
    nota = ring.encrypt("sospecha oncológica", field="consultations.internal_note")

    with pytest.raises(ClinicalCryptoError, match="otra columna"):
        ring.decrypt(nota, field=FIELD)


def test_texto_manipulado_no_descifra(ring: Keyring) -> None:
    ct = ring.encrypt("x", field=FIELD)
    kid, blob = ct.removeprefix("enc:v1:").split(":")
    raw = bytearray(base64.urlsafe_b64decode(blob))
    raw[-1] ^= 1
    tampered = f"enc:v1:{kid}:{base64.urlsafe_b64encode(bytes(raw)).decode()}"

    with pytest.raises(ClinicalCryptoError):
        ring.decrypt(tampered, field=FIELD)


def test_rotacion_descifra_con_la_anterior_y_cifra_con_la_nueva() -> None:
    vieja, nueva = generate_key(), generate_key()
    ct_viejo = Keyring.from_config(vieja).encrypt("hta", field=FIELD)

    rotado = Keyring.from_config(nueva, previous=vieja)

    assert rotado.decrypt(ct_viejo, field=FIELD) == "hta"
    assert clinical_crypto.ciphertext_kid(rotado.encrypt("hta", field=FIELD)) == rotado.active_kid
    with pytest.raises(ClinicalCryptoError, match="clave desconocida"):
        Keyring.from_config(nueva).decrypt(ct_viejo, field=FIELD)


@pytest.mark.parametrize(
    "raw",
    ["no-es-base64!!", base64.b64encode(b"corta").decode(), base64.b64encode(os.urandom(16))],
)
def test_clave_invalida_falla_al_cargar(raw) -> None:
    with pytest.raises(ClinicalCryptoError):
        Keyring.from_config(raw if isinstance(raw, str) else raw.decode())


def test_formato_invalido(ring: Keyring) -> None:
    with pytest.raises(ClinicalCryptoError, match="Formato"):
        ring.decrypt("enc:v1:sin-separador", field=FIELD)


# --- Sealed ---


def test_sealed_no_se_convierte_solo_a_texto() -> None:
    sealed = Sealed.from_db(FIELD, clinical_crypto.keyring().encrypt("VIH", field=FIELD))

    assert str(sealed) == REDACTED
    assert repr(sealed) == REDACTED
    assert f"motivo: {sealed}" == f"motivo: {REDACTED}"
    assert sealed != "VIH"  # comparar con texto no revela nada por canal lateral
    assert reveal(sealed) == "VIH"


def test_sealed_legado_en_claro_se_trata_igual() -> None:
    legado = Sealed.from_db(FIELD, "motivo de antes del backfill")

    assert legado.ciphertext is None
    assert str(legado) == REDACTED
    assert legado.reveal() == "motivo de antes del backfill"
    assert legado.ciphertext_for(FIELD).startswith("enc:v1:")


def test_sealed_reutiliza_en_su_columna_y_recifra_en_otra() -> None:
    ct = clinical_crypto.keyring().encrypt("alergia a penicilina", field="patients.description")
    sealed = Sealed.from_db("patients.description", ct)

    assert sealed.ciphertext_for("patients.description") == ct
    movido = sealed.ciphertext_for(FIELD)
    assert movido != ct
    assert clinical_crypto.keyring().decrypt(movido, field=FIELD) == "alergia a penicilina"


def test_sealed_igualdad_y_hash() -> None:
    ct = clinical_crypto.keyring().encrypt("a", field=FIELD)
    a, b = Sealed.from_db(FIELD, ct), Sealed.from_db(FIELD, ct)

    assert a == b and hash(a) == hash(b)
    assert a != Sealed.from_db(FIELD, clinical_crypto.keyring().encrypt("a", field=FIELD))


def test_reveal_de_none_y_de_texto_recien_escrito() -> None:
    assert reveal(None) is None
    assert reveal("recién escrito en esta petición") == "recién escrito en esta petición"


# --- Esquemas: descifrado solo con permiso ---


class _Out(ClinicalAccessMixin):
    motivo: ClinicalSummary = None
    nota: ClinicalNote = None


class _Row:
    def __init__(self, motivo, nota) -> None:
        self.motivo = motivo
        self.nota = nota


def _row() -> _Row:
    ring = clinical_crypto.keyring()
    return _Row(
        Sealed.from_db(FIELD, ring.encrypt("fiebre", field=FIELD)),
        Sealed.from_db("x.nota", ring.encrypt("descartar dengue", field="x.nota")),
    )


def test_sin_contexto_todo_en_null() -> None:
    out = _Out.model_validate(_row(), from_attributes=True)

    assert out.model_dump() == {"clinical_access": "none", "motivo": None, "nota": None}


def test_summary_ve_el_motivo_pero_no_las_notas() -> None:
    out = _Out.model_validate(
        _row(), from_attributes=True, context=clinical_context(summary_grant("queue_scope"))
    )

    assert out.model_dump() == {"clinical_access": "summary", "motivo": "fiebre", "nota": None}


def test_tratante_ve_todo() -> None:
    out = _Out.model_validate(
        _row(), from_attributes=True, context=clinical_context(treating_grant("assigned_doctor"))
    )

    assert out.model_dump() == {
        "clinical_access": "full",
        "motivo": "fiebre",
        "nota": "descartar dengue",
    }


def test_valor_indescifrable_va_en_null_y_no_se_loguea_el_contenido(caplog) -> None:
    ajena = Keyring.from_config(generate_key()).encrypt("secreto", field=FIELD)
    row = _Row(Sealed.from_db(FIELD, ajena), None)

    with caplog.at_level(logging.ERROR, logger="mpv.api"):
        out = _Out.model_validate(
            row, from_attributes=True, context=clinical_context(treating_grant("assigned_doctor"))
        )

    assert out.motivo is None
    assert "indescifrable" in caplog.text
    assert "secreto" not in caplog.text


def test_campo_clinico_rechaza_otros_tipos() -> None:
    with pytest.raises(ValidationError):
        _Out.model_validate(
            _Row(123, None),
            from_attributes=True,
            context=clinical_context(treating_grant("assigned_doctor")),
        )


def test_un_str_normal_rechaza_un_sealed() -> None:
    """Un esquema que olvidó usar ClinicalSummary falla (500) en vez de filtrar el texto."""

    class _Olvidado(BaseModel):
        motivo: str

    with pytest.raises(ValidationError):
        _Olvidado.model_validate({"motivo": _row().motivo})


# --- Tipo de columna contra la base real ---


async def _patient(session: AsyncSession, **fields) -> Patient:
    owner = Profile(id=uuid.uuid4(), full_name="Médico Test", role="doctor", role_chosen=True)
    session.add(owner)
    await session.flush()
    patient = Patient(full_name="Paciente Cifrado", created_by_doctor_id=owner.id, **fields)
    session.add(patient)
    await session.flush()
    return patient


async def _raw(session: AsyncSession, patient_id: uuid.UUID) -> tuple[str | None, str | None]:
    row = (
        await session.execute(
            text("select description, allergies from patients where id = :id"),
            {"id": patient_id},
        )
    ).one()
    return row.description, row.allergies


async def test_la_base_guarda_texto_cifrado_y_el_orm_devuelve_sealed(
    db_session: AsyncSession,
) -> None:
    patient = await _patient(db_session, description="cáncer de mama 2019", allergies=None)

    raw_description, raw_allergies = await _raw(db_session, patient.id)
    assert raw_description.startswith("enc:v1:")
    assert "cáncer" not in raw_description
    assert raw_allergies is None

    patient_id = patient.id
    db_session.expire(patient)
    reloaded = await db_session.scalar(select(Patient).where(Patient.id == patient_id))
    assert isinstance(reloaded.description, Sealed)
    assert reveal(reloaded.description) == "cáncer de mama 2019"


async def test_actualizar_otra_columna_no_recifra_el_campo(db_session: AsyncSession) -> None:
    patient = await _patient(db_session, description="asma")
    patient_id = patient.id
    antes, _ = await _raw(db_session, patient_id)
    db_session.expire(patient)
    reloaded = await db_session.scalar(select(Patient).where(Patient.id == patient_id))

    reloaded.full_name = "Otro nombre"
    await db_session.flush()

    despues, _ = await _raw(db_session, patient_id)
    assert despues == antes


# --- Backfill ---


async def _asyncpg(session: AsyncSession):
    """La conexión asyncpg de la transacción del test: el script ve (y deshace) lo mismo."""
    conn = await session.connection()
    raw = await conn.get_raw_connection()
    return raw.driver_connection


async def test_backfill_cifra_lo_legado_y_recifra_lo_rotado(
    db_session: AsyncSession, monkeypatch
) -> None:
    vieja_key = generate_key()
    vieja = Keyring.from_config(vieja_key)
    patient = await _patient(db_session)
    await db_session.execute(
        text("update patients set description = :d, allergies = :a where id = :id"),
        {
            "d": "diabetes tipo 2",  # legado en claro
            "a": vieja.encrypt("látex", field="patients.allergies"),  # clave anterior
            "id": patient.id,
        },
    )
    rotado = Keyring.from_config(generate_key(), previous=vieja_key)
    monkeypatch.setattr(clinical_crypto, "keyring", lambda: rotado)
    conn = await _asyncpg(db_session)
    targets = {t.field: t for t in backfill.encrypted_targets()}

    for field in ("patients.description", "patients.allergies"):
        await backfill.process(conn, targets[field], batch_size=50, dry_run=False)

    description, allergies = await _raw(db_session, patient.id)
    assert clinical_crypto.ciphertext_kid(description) == rotado.active_kid
    assert clinical_crypto.ciphertext_kid(allergies) == rotado.active_kid
    assert rotado.decrypt(description, field="patients.description") == "diabetes tipo 2"
    assert rotado.decrypt(allergies, field="patients.allergies") == "látex"

    # Segunda corrida: nada pendiente para esta fila (idempotente).
    pending = await conn.fetch(
        backfill._pending_sql(targets["patients.description"]),
        f"enc:v1:{rotado.active_kid}:%",
        "00000000-0000-0000-0000-000000000000",
        10_000,
    )
    assert patient.id not in {r["id"] for r in pending}


def test_backfill_cubre_todas_las_columnas_cifradas() -> None:
    fields = {t.field for t in backfill.encrypted_targets()}

    assert {
        "consultations.chief_complaint",
        "consultations.internal_note",
        "consultations.clinical_notes",
        "consultation_events.note",
        "patients.description",
        "patients.allergies",
        "prescriptions.medications",
        "treatment_plans.plan",
    } <= fields


def test_generate_key_imprime_una_clave_valida(capsys) -> None:
    assert backfill.main(["--generate-key"]) == 0
    Keyring.from_config(capsys.readouterr().out.strip())


async def test_backfill_no_aborta_por_una_fila_indescifrable(
    db_session: AsyncSession,
) -> None:
    """Una fila cifrada con una clave ajena se informa por id y la corrida sigue."""
    rota = await _patient(db_session)
    sana = await _patient(db_session)
    ajena = Keyring.from_config(generate_key()).encrypt("x", field="patients.description")
    await db_session.execute(
        text("update patients set description = :d where id = :id"), {"d": ajena, "id": rota.id}
    )
    await db_session.execute(
        text("update patients set description = 'legado' where id = :id"), {"id": sana.id}
    )
    conn = await _asyncpg(db_session)
    target = next(t for t in backfill.encrypted_targets() if t.field == "patients.description")

    result = await backfill.process(conn, target, batch_size=50, dry_run=False)

    assert str(rota.id) in result.unreadable
    sana_raw, _ = await _raw(db_session, sana.id)
    assert sana_raw.startswith("enc:v1:")


def test_backfill_se_niega_a_usar_la_clave_de_desarrollo_contra_una_base_remota(
    monkeypatch,
) -> None:
    from src.core.config import settings
    from src.main import _INSECURE_CLINICAL_KID

    assert backfill.key_guard(_INSECURE_CLINICAL_KID, None) is None  # local: vale

    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://prod.example/db")
    assert "DESARROLLO" in backfill.key_guard(_INSECURE_CLINICAL_KID, None)
    assert backfill.key_guard("abcd1234", None) is None
    assert "no deadbeef" in backfill.key_guard("abcd1234", "deadbeef")


async def test_decrypt_deja_en_claro_y_el_ida_y_vuelta_no_pierde_nada(
    db_session: AsyncSession,
) -> None:
    """Rollback de emergencia: --decrypt devuelve exactamente el texto original, y volver a
    cifrar después funciona (el ensayo de prod hace esta misma ida y vuelta)."""
    patient = await _patient(db_session, description="cáncer de mama 2019", allergies="látex")
    conn = await _asyncpg(db_session)
    targets = [t for t in backfill.encrypted_targets() if t.table == "patients"]

    for t in targets:
        await backfill.process(conn, t, batch_size=50, dry_run=False, decrypt=True)
    assert await _raw(db_session, patient.id) == ("cáncer de mama 2019", "látex")

    for t in targets:
        await backfill.process(conn, t, batch_size=50, dry_run=False)
    description, allergies = await _raw(db_session, patient.id)
    assert description.startswith("enc:v1:") and allergies.startswith("enc:v1:")
    ring = clinical_crypto.keyring()
    assert ring.decrypt(description, field="patients.description") == "cáncer de mama 2019"


def test_decrypt_exige_confirmacion(capsys) -> None:
    assert backfill.main(["--decrypt"]) == 2
    assert "--yes" in capsys.readouterr().err


async def test_backfill_no_pisa_lo_que_la_api_escribe_durante_la_corrida(
    db_session: AsyncSession,
) -> None:
    """Entre leer el lote y escribirlo, la API cambia una fila (un médico edita la nota): el
    UPDATE condicional no la pisa con el valor viejo cifrado, la cuenta como `raced`."""
    patient = await _patient(db_session)
    await db_session.execute(
        text("update patients set description = 'legado' where id = :id"), {"id": patient.id}
    )
    real = await _asyncpg(db_session)
    nuevo = clinical_crypto.keyring().encrypt("editado por la API", field="patients.description")

    class _ApiEscribeEnMedio:
        """Proxy de la conexión: justo antes del UPDATE del lote, la 'API' escribe la fila."""

        async def fetch(self, sql, *args):
            if sql.lstrip().startswith("update"):
                await real.execute(
                    "update patients set description = $1 where id = $2", nuevo, patient.id
                )
            return await real.fetch(sql, *args)

    target = next(t for t in backfill.encrypted_targets() if t.field == "patients.description")
    result = await backfill.process(_ApiEscribeEnMedio(), target, batch_size=50, dry_run=False)

    assert result.raced >= 1
    raw, _ = await _raw(db_session, patient.id)
    assert raw == nuevo  # lo de la API sobrevive


# --- Auditoría de las corridas del script ---


class _SinCerrar:
    """La conexión del test para `run()`: misma transacción (todo se deshace) y `close()` no-op."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def close(self) -> None:
        return None


def _args(**overrides):
    import argparse

    base = {
        "dry_run": False,
        "verify": False,
        "vacuum": False,
        "decrypt": False,
        "yes": False,
        "operator": None,
        "expect_kid": None,
        "batch_size": 200,
    }
    return argparse.Namespace(**{**base, **overrides})


async def _corridas(session: AsyncSession) -> list:
    """Filas de las corridas del script, en orden de escritura (el id no es secuencial, pero dentro
    de una misma transacción `created_at` es igual: se desempata por la fase)."""
    stmt = text(
        "select action, actor_user_id, metadata, correlation_id from audit_log "
        "where resource = 'clinical_data' "
        "order by created_at, array_position(array['started','finished','failed'], "
        "metadata->>'phase')"
    )
    return (await session.execute(stmt)).all()


def test_escribir_sin_operador_no_corre(capsys) -> None:
    """Cifrar o descifrar sin decir quién lo hace no toca la base (sale antes de conectar)."""
    assert backfill.main([]) == 2
    assert "--operator" in capsys.readouterr().err
    assert backfill.main(["--decrypt", "--yes"]) == 2
    assert "--operator" in capsys.readouterr().err


async def test_corrida_queda_auditada_al_empezar_y_al_terminar(
    db_session: AsyncSession, monkeypatch
) -> None:
    import asyncpg

    operador = "operador.cifrado@example.org"
    cuenta = Profile(
        id=uuid.uuid4(), full_name="Operador", role="admin", role_chosen=True, email=operador
    )
    db_session.add(cuenta)
    patient = await _patient(db_session)
    await db_session.execute(
        text("update patients set allergies = 'legado' where id = :id"), {"id": patient.id}
    )
    conn = await _asyncpg(db_session)

    async def _connect(**_):
        return _SinCerrar(conn)

    monkeypatch.setattr(asyncpg, "connect", _connect)

    assert await backfill.run(_args(operator=operador)) == 0

    filas = await _corridas(db_session)
    assert [f.metadata["phase"] for f in filas][-2:] == ["started", "finished"]
    inicio, fin = filas[-2], filas[-1]
    assert inicio.correlation_id == fin.correlation_id
    assert inicio.action == fin.action == "clinical_data.bulk_encrypt"
    assert inicio.actor_user_id == cuenta.id  # el correo se cruza con su cuenta
    assert fin.metadata["operator"] == operador
    assert fin.metadata["counts"]["patients.allergies"] >= 1
    assert fin.metadata["kid"] == clinical_crypto.keyring().active_kid
    raw = await db_session.scalar(
        text("select allergies from patients where id = :id"), {"id": patient.id}
    )
    assert raw.startswith("enc:v1:")


async def test_corrida_que_falla_deja_constancia(db_session: AsyncSession, monkeypatch) -> None:
    import asyncpg

    conn = await _asyncpg(db_session)

    async def _connect(**_):
        return _SinCerrar(conn)

    async def _revienta(*_, **__):
        raise RuntimeError("se cayó a mitad")

    monkeypatch.setattr(asyncpg, "connect", _connect)
    monkeypatch.setattr(backfill, "process", _revienta)

    with pytest.raises(RuntimeError):
        await backfill.run(_args(decrypt=True, yes=True, operator="alguien@example.org"))

    filas = await _corridas(db_session)
    assert [f.metadata["phase"] for f in filas][-2:] == ["started", "failed"]
    assert filas[-1].action == "clinical_data.bulk_decrypt"
    assert filas[-1].metadata["error"] == "RuntimeError"
    assert filas[-1].actor_user_id is None  # correo sin cuenta: queda solo en metadata


async def test_contar_o_verificar_no_escribe_audit(db_session: AsyncSession, monkeypatch) -> None:
    import asyncpg

    conn = await _asyncpg(db_session)

    async def _connect(**_):
        return _SinCerrar(conn)

    monkeypatch.setattr(asyncpg, "connect", _connect)
    antes = len(await _corridas(db_session))

    await backfill.run(_args(dry_run=True))
    await backfill.run(_args(decrypt=True, verify=True))

    assert len(await _corridas(db_session)) == antes
