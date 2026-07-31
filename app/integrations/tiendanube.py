from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


class TiendanubeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TiendanubeHealth:
    configured: bool
    connected: bool
    message: str


class TiendanubeClient:
    """Least-privilege Tiendanube API client with a conservative rate limiter."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0

    @property
    def configured(self) -> bool:
        token = self.settings.tiendanube_access_token
        return bool(
            self.settings.tiendanube_enabled
            and self.settings.tiendanube_store_id
            and token
            and token.get_secret_value().strip()
        )

    async def _throttle(self) -> None:
        # Tiendanube documents 2 requests/second. Use 1.8 to retain a buffer.
        async with self._rate_lock:
            wait = (1 / 1.8) - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def get(self, resource: str, *, params: dict[str, Any] | None = None) -> Any:
        if not self.configured:
            raise TiendanubeError("Tiendanube no esta configurada")
        await self._throttle()
        store_id = self.settings.tiendanube_store_id.strip()
        resource_path = resource.strip("/")
        token = self.settings.tiendanube_access_token.get_secret_value().strip()
        urls = [
            (
                f"{self.settings.tiendanube_api_base_url.rstrip('/')}/"
                f"{store_id}/{resource_path}"
            )
        ]
        legacy_url = f"https://api.tiendanube.com/v1/{store_id}/{resource_path}"
        if legacy_url not in urls:
            urls.append(legacy_url)

        headers = {
            # Tiendanube/Nuvemshop has shown both contracts in different
            # onboarding screens. Keep both read-only auth headers so the same
            # token works with current and legacy API routes.
            "Authorization": f"Bearer {token}",
            "Authentication": f"bearer {token}",
            "User-Agent": self.settings.tiendanube_user_agent,
            "Accept": "application/json",
        }
        response: httpx.Response | None = None
        for url_index, url in enumerate(urls):
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(
                        timeout=self.settings.tiendanube_request_timeout_seconds,
                        transport=self.transport,
                        trust_env=False,
                    ) as client:
                        response = await client.get(url, params=params, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    raise TiendanubeError(
                        f"Tiendanube no responde ({type(exc).__name__})"
                    ) from exc
                if response.status_code != 429:
                    break
                await asyncio.sleep(min(2**attempt, 4))

            # If the versioned API rejects the token/route, try Tiendanube's
            # legacy onboarding example before surfacing the error.
            if not (url_index == 0 and response.status_code in {401, 404}):
                break

        assert response is not None
        if response.status_code >= 400:
            raise TiendanubeError(
                f"Tiendanube devolvio un error ({response.status_code}): "
                f"{self._safe_error_detail(response)}",
                status_code=response.status_code,
            )
        return response.json()

    def _safe_error_detail(self, response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            body_text = response.text.strip().replace("\n", " ")
            return body_text[:240] if body_text else "sin detalle"

        if isinstance(body, dict):
            detail = (
                body.get("message")
                or body.get("error_description")
                or body.get("error")
                or body.get("description")
                or body
            )
            return str(detail)[:240]
        return str(body)[:240]

    async def store(self) -> Any:
        return await self.get("store")

    async def products(self, *, page: int = 1, per_page: int = 50) -> Any:
        return await self.get("products", params={"page": max(1, page), "per_page": per_page})

    async def orders(self, *, page: int = 1, per_page: int = 50) -> Any:
        normalized_page = max(1, page)
        try:
            return await self.get(
                "orders",
                params={"page": normalized_page, "per_page": per_page},
            )
        except TiendanubeError as exc:
            # Tiendanube returns 404 instead of an empty array when the caller
            # advances past the final page.  That is a normal pagination
            # boundary, but only after at least one page was read.
            if normalized_page > 1 and exc.status_code == 404:
                return []
            raise

    async def customers(self, *, page: int = 1, per_page: int = 50) -> Any:
        normalized_page = max(1, page)
        try:
            return await self.get(
                "customers",
                params={"page": normalized_page, "per_page": per_page},
            )
        except TiendanubeError as exc:
            if normalized_page > 1 and exc.status_code == 404:
                return []
            raise

    async def health(self) -> TiendanubeHealth:
        if not self.configured:
            return TiendanubeHealth(False, False, "Credenciales pendientes en Dokploy")
        try:
            await self.products(page=1, per_page=1)
        except TiendanubeError as exc:
            return TiendanubeHealth(True, False, str(exc))
        return TiendanubeHealth(True, True, "Tiendanube conectada en modo solo lectura")
