# Ecosistema, Calidad de Código y Testing Avanzado

## 📦 Gestión del Entorno (uv)
El proyecto utiliza estrictamente **uv** (de Astral) como gestor de paquetes por su alto rendimiento. No utilices pip ni poetry de manera directa.
- Sincronizar dependencias: `uv sync`
- Agregar un paquete: `uv add <paquete>`
- Ejecutar servidor de desarrollo local: `uv run uvicorn src.main:app --reload --workers 1`

## 🧪 Estándar Riguroso de Testing (Pytest + Asyncio)
- Las pruebas que involucren peticiones de red o base de datos deben ser asíncronas utilizando el decorador `@pytest.mark.asyncio(scope="session")`.
- **Aislamiento de Datos mediante Savepoints:** Los tests mutativos deben correr dentro de transacciones anidadas (savepoints de PostgreSQL). Al finalizar cada test, se debe aplicar un `rollback` automático de la sesión de pruebas para garantizar un entorno limpio y libre de datos basura entre ejecuciones.
- **Prueba de Colisión Concurrente:** Es obligatorio escribir pruebas de estrés de concurrencia que utilicen `asyncio.gather` y `httpx.AsyncClient` para simular peticiones simultáneas sobre el mismo paciente en la cola, validando que el backend asigne correctamente el estado 200 al ganador y el estado 409 al perdedor.
- Ejecutar la suite con reporte de cobertura (Mínimo requerido: 95%):
  ```bash
  uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto
  ```
- **⚠️ Cobertura con SQLAlchemy async:** el trabajo de BD corre dentro de **greenlets**. Sin
  `concurrency = ["thread", "greenlet"]` en `[tool.coverage.run]`, coverage reporta falsos
  negativos (servicios al 0%). Ya está configurado en `pyproject.toml`.
- Los tests requieren el Postgres local activo: `docker compose up -d db`.

## 🎨 Lint y Formato (Ruff)
Al terminar cualquier cambio, deja el código limpio:
```bash
uv run ruff check . --fix
uv run ruff format .
```
- `B008` está ignorado a propósito: `Depends()`/`Query()` como valor por defecto es el patrón
  idiomático de FastAPI, no un error.

## 📖 Documentación de la API (Swagger / OpenAPI)
La doc interactiva es parte del entregable (los devs la leen para entender los contratos):
- Swagger UI: `http://localhost:8000/docs` · ReDoc: `/redoc` · esquema: `/api/v1/openapi.json`.
- Todo endpoint nuevo debe llevar `summary=`, un docstring (se vuelve la descripción) y los
  `responses=` de sus códigos de error (404/409/422). Agrupa por `tags`.

## ✅ Definition of Done
Un cambio NO está listo hasta que: pasan los tests, cobertura ≥95%, `ruff check`/`format` limpios,
y los endpoints nuevos quedan documentados en Swagger.
