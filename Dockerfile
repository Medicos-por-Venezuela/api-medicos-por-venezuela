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

# Usuario sin privilegios.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
