"""Endpoints de la sesión autenticada (la identidad sale del JWT de Supabase)."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.errors import NotFoundError
from src.core.security import Principal, get_current_principal, issue_access_token
from src.db.session import get_db
from src.schemas.auth_dev import DevAuthResponse, DevLoginRequest, DevRegisterRequest
from src.schemas.profile import ProfileResponse
from src.services import auth_dev as auth_dev_service
from src.services import profiles as profiles_service

router = APIRouter(prefix="/auth", tags=["auth"])
tag_metadata = [
    {"name": "auth", "description": "Sesión autenticada: la identidad sale del JWT de Supabase."}
]


@router.get(
    "/me",
    response_model=ProfileResponse,
    summary="Perfil del usuario autenticado",
    responses={401: {"description": "No autenticado / token inválido."}},
)
async def me(
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ProfileResponse:
    """Devuelve el perfil del titular del JWT (reemplaza getSession + cargar profile)."""
    return await profiles_service.get_profile(db, principal.id)


@router.post(
    "/dev/register",
    response_model=DevAuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registro de DEV (solo local; sustituye el signup de Supabase Auth)",
    responses={404: {"description": "No disponible en producción."}},
)
async def dev_register(
    payload: DevRegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> DevAuthResponse:
    """Crea (o recupera) un usuario en la BD local y devuelve un JWT de sesión, **sin**
    llamar a Supabase Auth. Pensado para pruebas del frontend en local; en producción
    el signup lo maneja Supabase y este endpoint responde 404."""
    if settings.ENVIRONMENT == "production":
        raise NotFoundError("Endpoint no disponible.")
    profile, created = await auth_dev_service.register_or_get(
        db,
        email=payload.email,
        full_name=payload.full_name,
        role=payload.role,
        specialty=payload.specialty,
        whatsapp_number=payload.whatsapp_number,
        country=payload.country,
        medical_license=payload.medical_license,
    )
    return DevAuthResponse(
        access_token=issue_access_token(profile.id),
        user_id=profile.id,
        role=profile.role,
        created=created,
    )


@router.post(
    "/dev/login",
    response_model=DevAuthResponse,
    summary="Login de DEV (solo local; token por email, sin password)",
    responses={404: {"description": "No disponible en producción / usuario inexistente."}},
)
async def dev_login(
    payload: DevLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> DevAuthResponse:
    """Emite un JWT para una cuenta existente (por email), **sin** Supabase Auth ni password.
    Solo local; en producción responde 404. Para re-loguear en pruebas del frontend."""
    if settings.ENVIRONMENT == "production":
        raise NotFoundError("Endpoint no disponible.")
    profile = await auth_dev_service.get_by_email(db, payload.email)
    if profile is None:
        raise NotFoundError("No existe una cuenta con ese email.")
    return DevAuthResponse(
        access_token=issue_access_token(profile.id),
        user_id=profile.id,
        role=profile.role,
        created=False,
    )
