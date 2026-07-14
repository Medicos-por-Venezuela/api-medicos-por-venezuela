FROM python:3.12-slim

# No generar .pyc y salida sin buffer (logs en tiempo real).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# uv (Astral) como gestor de paquetes.
RUN pip install --no-cache-dir uv

# El driver es asyncpg (protocolo puro, sin libpq), así que no se requieren
# dependencias de sistema adicionales.
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .

# Runner de migraciones + CLI, para correr `python artisan migrate` dentro del
# contenedor en el EC2 (asyncpg + config ya vienen con la app; no hay .venv, así
# que artisan usa el python del sistema). Se copian después del install para no
# invalidar la capa de dependencias al cambiar una migración.
COPY db ./db
COPY scripts ./scripts
COPY artisan ./artisan

# Usuario sin privilegios.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
