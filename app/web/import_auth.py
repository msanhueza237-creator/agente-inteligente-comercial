"""Read-only authorization of historical previews using the current CRM session."""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings


async def import_auth_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        yield client


async def require_historical_import_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[httpx.AsyncClient, Depends(import_auth_client)],
) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip() or len(token) > 16384:
        raise HTTPException(401, "Inicia sesión en el CRM para analizar archivos.")
    if (not settings.import_supabase_url or not settings.import_supabase_anon_key
            or not settings.import_supabase_anon_key.get_secret_value().strip()):
        raise HTTPException(503, "El servicio de importación aún no está configurado.")

    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "apikey": settings.import_supabase_anon_key.get_secret_value(),
    }
    try:
        user_response = await client.get(
            f"{settings.import_supabase_url}/auth/v1/user", headers=headers
        )
        if user_response.status_code in {401, 403}:
            raise HTTPException(401, "La sesión expiró o no es válida. Vuelve a iniciar sesión.")
        if user_response.status_code != 200:
            raise HTTPException(503, "No fue posible verificar la sesión del CRM.")
        user = user_response.json()
        if not isinstance(user, dict) or not isinstance(user.get("id"), str):
            raise TypeError("Invalid user response")
        user_id = str(UUID(user["id"]))
        profile_response = await client.get(
            f"{settings.import_supabase_url}/rest/v1/profiles",
            headers=headers,
            params={"id": f"eq.{user_id}", "select": "role,active", "limit": "1"},
        )
        if profile_response.status_code in {401, 403}:
            raise HTTPException(403, "No tienes permiso para importar archivos históricos.")
        if profile_response.status_code != 200:
            raise HTTPException(503, "No fue posible verificar los permisos del CRM.")
        profiles = profile_response.json()
        if not isinstance(profiles, list):
            raise TypeError("Invalid profile response")
        profile = profiles[0] if profiles else {}
        if (
            not isinstance(profile, dict)
            or profile.get("active") is not True
            or profile.get("role") not in {"administrador", "vendedor"}
        ):
            raise HTTPException(403, "No tienes permiso para importar archivos históricos.")
        return user_id
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        # Do not expose upstream bodies, tokens, credentials or internal URLs.
        raise HTTPException(503, "No fue posible verificar el acceso al CRM.") from exc
