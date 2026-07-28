from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


class TiendanubeError(RuntimeError):
    pass


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
        url = (
            f"{self.settings.tiendanube_api_base_url.rstrip('/')}/"
            f"{store_id}/{resource.strip('/')}"
        )
        headers = {
            "Authentication": (
                f"bearer {self.settings.tiendanube_access_token.get_secret_value()}"
            ),
            "User-Agent": self.settings.tiendanube_user_agent,
            "Accept": "application/json",
        }
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
        if response.status_code >= 400:
            raise TiendanubeError(f"Tiendanube devolvio un error ({response.status_code})")
        return response.json()

    async def store(self) -> Any:
        return await self.get("store")

    async def products(self, *, page: int = 1, per_page: int = 50) -> Any:
        return await self.get("products", params={"page": max(1, page), "per_page": per_page})

    async def orders(self, *, page: int = 1, per_page: int = 50) -> Any:
        return await self.get("orders", params={"page": max(1, page), "per_page": per_page})

    async def customers(self, *, page: int = 1, per_page: int = 50) -> Any:
        return await self.get("customers", params={"page": max(1, page), "per_page": per_page})

    async def health(self) -> TiendanubeHealth:
        if not self.configured:
            return TiendanubeHealth(False, False, "Credenciales pendientes en Dokploy")
        try:
            await self.products(page=1, per_page=1)
        except TiendanubeError as exc:
            return TiendanubeHealth(True, False, str(exc))
        return TiendanubeHealth(True, True, "Tiendanube conectada en modo solo lectura")
