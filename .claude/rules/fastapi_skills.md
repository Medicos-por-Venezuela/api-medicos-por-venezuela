# Habilidades de Programación en FastAPI y Control de Flujo

## 🏗️ Arquitectura de Software (Service Layer Pattern)
Utiliza estrictamente el "Patrón de Capa de Servicios" (3-Tier Architecture). El código debe estar separado de la siguiente manera:
1. **Routers (`src/routers/`):** Capa HTTP. Estrictamente "delgados". Solo reciben peticiones, inyectan dependencias (como `AsyncSession` mediante `Depends`), llaman al servicio correspondiente y manejan excepciones convirtiéndolas en respuestas HTTP (ej. `HTTPException`). ¡CERO consultas a la base de datos o lógica de negocio aquí!
2. **Services (`src/services/`):** Capa de Negocio. Aquí viven las funciones asíncronas puras que contienen la lógica, bloqueos y consultas a SQLAlchemy. Reciben los datos planos y la sesión (`session: AsyncSession`) por parámetro. No saben qué es un Request, ni devuelven HTTPExceptions. Si hay un error de negocio, lanzan excepciones nativas de Python (`ValueError`, `PermissionError`, `OperationalError`).
3. **Schemas (`src/schemas/`):** Capa de Validación. Modelos de Pydantic v2 para validar entrada/salida.
4. **Models (`src/models/`):** Capa de Base de Datos. Modelos declarativos de SQLAlchemy 2.0.

## ⚡ Concurrencia Nativa y Async
- Toda ruta de API que interactúe con base de datos, colas de mensajería o servicios de Supabase debe ser declarada utilizando `async def`.
- No bloquees el event loop de FastAPI con funciones síncronas pesadas. Si requieres usar código síncrono, delégalo usando `run_in_executor`.

## ⚙️ Configuración Tipada (Pydantic Settings)
- Prohibido el uso de `os.environ` o `os.getenv` en el código.
- Toda variable de entorno debe estar definida y validada en una clase `Settings(BaseSettings)` en `src/core/config.py`.
- Utiliza el caché de `lru_cache` o dependencias de FastAPI para instanciar la configuración y leerla de forma segura.

## 👁️ Observabilidad y Logs Estructurados
- Implementa un Middleware que genere un `X-Correlation-ID` (UUID) para cada petición entrante.
- Usa una librería de logging estructurado (como `structlog` o configurando el logger estándar a JSON) que inyecte este Correlation ID en cada mensaje.
- Prohibido usar `print()`.

## 📄 Paginación Segura
- NUNCA utilices el método `.all()` en SQLAlchemy para devolver listas de registros.
- Todo endpoint tipo `GET` que retorne colecciones debe implementar paginación mediante `limit` y `offset` por defecto (máximo 100 registros por página).
- **Todo `order_by` de un listado paginado termina en una columna única** (p.ej. `.order_by(X.nombre, X.id)`): sin tiebreaker, el orden entre empatados es indefinido y `OFFSET` puede repetir/omitir filas entre páginas.
- **El test del tiebreaker debe ser determinista, no probabilístico**: no basta "recorrer páginas sin duplicados" (los empates suelen salir en orden repetible por accidente y el test no detecta la regresión). Siembra N filas empatadas y asierta que aparecen **en el orden de la columna de desempate** (lección del review 2026-07-14, ver `test_pool_paginacion_disjunta_y_total`).

## ⏱️ Resiliencia y Background Tasks
- Diseña estados transitorios. Si una consulta no se completa o actualiza en X minutos, debe existir un mecanismo (BackgroundTasks de FastAPI o un CRON worker) que libere al paciente, devolviéndolo al estado `esperando`.

## 🛡️ Manejo Global de Excepciones de Base de Datos
- No utilices bloques `try/except` repetitivos en los endpoints para capturar errores de base de datos.
- Configura manejadores de excepciones globales (`@app.exception_handler`) en el archivo principal de la aplicación (`main.py`) para capturar excepciones nativas de SQLAlchemy como `OperationalError` e `IntegrityError`.
- El manejador debe traducir de forma automática estos fallos de concurrencia interna en respuestas HTTP semánticas estandarizadas (como `409 Conflict`), protegiendo los detalles internos de la base de datos de cara al cliente.

## 📋 Validación con Pydantic v2
- Divide de forma estricta los esquemas de datos: `PacienteCreate`, `PacienteUpdate` y `PacienteResponse`.
- Activa `from_attributes = True` dentro del objeto `model_config` en tus clases Pydantic para habilitar la serialización directa de los modelos ORM de SQLAlchemy.
- Utiliza los validadores nativos de Pydantic (`EmailStr`, `Field(..., min_length=2)`) para asegurar que los datos estén limpios antes de enviarlos a los servicios.

## 🏗️ Modularización de la API
- Estructura las rutas utilizando `APIRouter` encapsulados por contexto o dominio en `src/routers/` (ej. `medicos.py`, `cola.py`).
- Implementa inyección de dependencias (`Depends()`) para inyectar de forma transparente sesiones de base de datos, esquemas de autenticación JWT y políticas de control de acceso basados en roles (RBAC).

## 🔁 Excepciones de dominio → HTTP (contrato)
Los **servicios** lanzan excepciones de dominio (`src/core/errors.py`); los **manejadores
globales** (`src/core/exceptions.py`) las traducen. No mapees a mano en cada router.

| Excepción de dominio (servicio) | HTTP | Cuándo |
| ------------------------------- | ---- | ------ |
| `NotFoundError`                 | 404  | El recurso no existe |
| `BadRequestError`               | 400  | Regla de negocio violada (ej. falta `consent`) |
| `ConflictError`                 | 409  | Conflicto de estado |
| `UnprocessableError`            | 422  | Dato válido sintácticamente pero no semánticamente (ej. `status` inválido) |
| `IntegrityError` (SQLAlchemy)   | 409  | UNIQUE / FOREIGN KEY |
| Lock `55P03` (asyncpg/DBAPIError)| 409 | Fila bloqueada (`with_for_update(nowait=True)`) |

- Única excepción permitida de `try/except` en un router: el catch del lock de la cola para dar
  un mensaje de dominio específico (ver `src/routers/queue.py`). Todo lo demás, vía global.

## 📖 Documentación obligatoria en Swagger
Todo endpoint debe ser autoexplicativo en `/docs` (los devs lo usan como contrato):
- `summary=` corto + **docstring** (se vuelve la descripción).
- `responses={...}` documentando los códigos de error (404/409/422) con su `description`.
- Agrupar con `tags`; describir cada tag en `openapi_tags` (en `main.py`).
- Esquemas Pydantic con nombres `*Create` / `*Update` / `*Response` (sirven de modelo en OpenAPI).
