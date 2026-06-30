# Reglas de Base de Datos y Concurrencia Avanzada

## 🌐 Gestión de Entornos
- **Local:** PostgreSQL con extensión de replicación lógica habilitada para simular el comportamiento de Supabase Realtime localmente. Las credenciales se leen desde `.env` mediante `DATABASE_URL`.
- **Producción:** Supabase (Connection Pooler en puerto 5432/6543 según tipo de sesión). Las mutaciones en tablas críticas disparan eventos nativos por CDC (Change Data Capture) hacia el Board.

## 🔒 Control de Concurrencia Crítica (Fallo Rápido / Nowait)
- **Bloqueo Pesimista Obligatorio:** Para evitar condiciones de carrera cuando dos médicos hacen clic en el mismo paciente en el mismo milisegundo, **NUNCA** realices un `select` común seguido de un `update`.
- **Uso de `nowait=True`:** Usa siempre `with_for_update(nowait=True)` en SQLAlchemy para bloquear la fila inmediatamente. Si la fila ya está bloqueada por otra transacción activa, la base de datos lanzará inmediatamente una excepción `OperationalError`, evitando que la petición HTTP se quede colgada esperando.

  ```python
  # Patrón de código requerido para transacciones de cola concurrentes:
  from sqlalchemy.exc import OperationalError

  try:
      stmt = (
          select(ColaPacientes)
          .where(ColaPacientes.id == paciente_id, ColaPacientes.estado == "esperando")
          .with_for_update(nowait=True)
      )
      result = await session.execute(stmt)
      turno = result.scalar_one_or_none()

      if not turno:
          raise HTTPException(status_code=404, detail="El turno ya no está disponible.")

      turno.estado = "en_consulta"
      turno.medico_id = medico_id
      await session.commit()
  except OperationalError:
      await session.rollback()
      raise HTTPException(
          status_code=409,
          detail="Este paciente está siendo seleccionado por otro médico en este momento. Inténtalo de nuevo."
      )
  ```

## ✅ Implementación real en este repo (lee esto antes de tocar la cola)

El patrón de arriba es la guía; así está **realmente** implementado (respeta esto):

- **Modelo real:** la "cola" es la tabla `consultations` con `status == "waiting"` (no existe
  `ColaPacientes`/`estado`). Al tomar un caso: `status -> "in_progress"`,
  `assigned_doctor_id`, `opened_at`/`started_at = now()`.
- **Separación capas:** el `select ... with_for_update(nowait=True)` y el `commit` viven en
  `src/services/queue.py` (capa de negocio). El servicio **lanza excepciones nativas**
  (no `HTTPException`). El router `src/routers/queue.py` mapea el error a HTTP.
- **⚠️ asyncpg ≠ psycopg2:** con el driver **asyncpg**, una fila bloqueada NO llega como
  `OperationalError`, sino como `sqlalchemy.exc.DBAPIError` cuyo `sqlstate` es **`55P03`**
  (`LockNotAvailableError`). El `except OperationalError` del ejemplo clásico **no lo captura**.
  Usa el helper `src.core.exceptions.is_lock_not_available(exc)` (detecta `55P03` venga de
  asyncpg o psycopg2) para traducirlo a `409`.
- **Manejo global:** además del catch específico en el router de la cola, hay manejadores
  globales en `src/core/exceptions.py` para `OperationalError`, `IntegrityError` y el lock
  `55P03` -> `409`.

## 🔌 Driver async y pooler de Supabase

- Driver: **asyncpg** (`postgresql+asyncpg://`). asyncpg **no** entiende `?sslmode=` en la URL;
  el SSL se pasa por `connect_args` (ver `src/core/config.py`).
- Detrás del **transaction pooler** de Supabase (puerto `6543`, PgBouncer) hay que desactivar
  los prepared statements: `connect_args={"statement_cache_size": 0,
  "prepared_statement_cache_size": 0}`. Ya está parametrizado por entorno.
- **Local (dev):** Postgres 17 en Docker (`docker compose`), datos reales restaurados desde un
  backup de Supabase. **Producción:** Supabase vía `DATABASE_URL`/`POSTGRES_*`.
