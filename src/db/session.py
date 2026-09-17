"""Motor y sesión asíncronos de SQLAlchemy."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import settings

# pool_pre_ping evita usar conexiones muertas detrás del pooler de Supabase.
engine = create_async_engine(
    settings.sqlalchemy_database_uri,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    connect_args=settings.connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependencia de FastAPI que entrega una sesión async y la cierra al terminar."""
    async with AsyncSessionLocal() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Dependencia que entrega la FÁBRICA de sesiones, no una sesión.

    Para los endpoints de larga duración (el SSE de la sala de espera): con `get_db` la sesión
    vive lo que dura la respuesta y retiene una conexión del pool por cada paciente esperando.
    Con la fábrica, el endpoint abre una sesión corta por lectura. Las pruebas la sustituyen para
    que esas sesiones vean los datos de su transacción."""
    return AsyncSessionLocal
