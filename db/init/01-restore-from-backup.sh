#!/bin/bash
# Restaura el backup más reciente de Supabase en el Postgres local.
# Se ejecuta automáticamente SOLO en la primera inicialización del contenedor
# (cuando el volumen pgdata está vacío) y solo si existe un .dump en /backups.
#
# Se ejecuta DESPUÉS de 00-supabase-stubs.sql, así que el esquema "auth", la
# función auth.uid() y los roles anon/authenticated ya existen.
#
# --disable-triggers: durante la carga desactiva los triggers, incluidas las FKs
# del sistema. Así los datos entran intactos aunque auth.users esté vacío
# (las FKs profiles.id / patients.user_id -> auth.users no aplican en local) y
# sin que el trigger generate_consultation_code regenere los códigos.

DUMP=$(ls -1t /backups/*.dump 2>/dev/null | head -n 1)

if [ -z "${DUMP}" ]; then
  echo "[restore] No hay backups en /backups (*.dump). Se omite la restauración."
  echo "[restore] La base queda solo con los stubs; carga datos luego con scripts/load_local.sh"
else
  echo "[restore] Restaurando ${DUMP} en la base ${POSTGRES_DB}..."
  # pg_restore devuelve código !=0 por errores ignorables (FKs a auth.users que
  # no se pueden añadir en local). No abortar la init por eso -> '|| true'.
  pg_restore \
    --no-owner \
    --no-privileges \
    --disable-triggers \
    -U "${POSTGRES_USER}" \
    -d "${POSTGRES_DB}" \
    "${DUMP}" || true
  echo "[restore] Restauración finalizada."
fi

# Las migraciones NO se aplican aquí: el contenedor de Postgres no tiene Python.
# El backup restaurado ya trae el esquema vigente de Supabase; el delta pendiente
# lo aplica el CLI desde el host/CI tras el arranque:
#   uv run python scripts/migrate.py up
# (ver README -> "Migraciones de esquema").
