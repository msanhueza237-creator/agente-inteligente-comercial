from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


class FactoError(RuntimeError):
    """Safe integration error that never includes credentials or response bodies."""


@dataclass(frozen=True)
class FactoHealth:
    configured: bool
    connected: bool
    message: str


class FactoClient:
    """Read-only Facto/Koywe client used by the Agent Hub.

    Authentication uses the resource-owner contract supplied by Facto. Tokens
    live only in memory and no method capable of mutating the ERP is exposed.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._access_token: str | None = None
        self._auth_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        values = (
            self.settings.facto_client_id,
            self.settings.facto_client_secret,
            self.settings.facto_username,
            self.settings.facto_password,
        )
        return self.settings.facto_enabled and all(
            value and value.get_secret_value().strip() for value in values
        )

    async def _authenticate(self) -> str:
        if not self.configured:
            raise FactoError("Facto no esta configurado")
        async with self._auth_lock:
            if self._access_token:
                return self._access_token
            body = {
                "grant_type": "password",
                "client_id": self.settings.facto_client_id.get_secret_value(),
                "client_secret": self.settings.facto_client_secret.get_secret_value(),
                "username": self.settings.facto_username.get_secret_value(),
                "password": self.settings.facto_password.get_secret_value(),
            }
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.facto_request_timeout_seconds,
                    transport=self.transport,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        f"{self.settings.facto_api_base_url.rstrip('/')}/auth",
                        json=body,
                        headers={"Accept": "application/json"},
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise FactoError(f"Facto no responde ({type(exc).__name__})") from exc
            if response.status_code >= 400:
                raise FactoError(f"Facto rechazo la autenticacion ({response.status_code})")
            payload = response.json()
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise FactoError("Facto no devolvio un token valido")
            self._access_token = token
            return token

    async def get(self, resource: str, *, params: dict[str, Any] | None = None) -> Any:
        token = await self._authenticate()
        path = resource.strip("/")
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.facto_request_timeout_seconds,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.get(
                    f"{self.settings.facto_api_base_url.rstrip('/')}/{path}",
                    params=params,
                    headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise FactoError(f"Facto no responde ({type(exc).__name__})") from exc
        if response.status_code == 401:
            self._access_token = None
            raise FactoError("La sesion de Facto expiro")
        if response.status_code >= 400:
            raise FactoError(f"Facto devolvio un error ({response.status_code})")
        return response.json()

    async def products(self, *, page: int = 1) -> Any:
        return await self.get("products", params={"page": max(1, page)})

    async def product(self, product_id: str | int) -> Any:
        return await self.get(f"products/{product_id}")

    async def customers(self, *, page: int = 1) -> Any:
        return await self.get("clients", params={"page": max(1, page)})

    async def documents(
        self,
        *,
        page: int = 1,
        per_page: int | None = None,
        issue_date_from: str | None = None,
        issue_date_to: str | None = None,
        order_by: str | None = None,
        document_status: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {"page": max(1, page)}
        if per_page is not None:
            params["per_page"] = max(1, min(per_page, 100))
        if issue_date_from:
            params["issue_date_from"] = issue_date_from
        if issue_date_to:
            params["issue_date_to"] = issue_date_to
        if order_by:
            params["order_by"] = order_by
        if document_status is not None:
            params["document_status"] = document_status
        return await self.get("documents", params=params)

    async def document(self, document_id: str | int) -> Any:
        return await self.get(f"documents/{document_id}")

    async def payments(self, *, page: int = 1, per_page: int | None = None) -> Any:
        """Try the read-only payment collection exposed by some Facto accounts.

        Facto's public reference documents POST /payments and GET
        /payments/{payment_id}, but not every account exposes a collection GET.
        The worker feature-detects this route and falls back to documentary
        credit exposure when it is unavailable.
        """

        params: dict[str, Any] = {"page": max(1, page)}
        if per_page is not None:
            params["per_page"] = max(1, min(per_page, 100))
        return await self.get("payments", params=params)

    async def payment(self, payment_id: str | int) -> Any:
        return await self.get(f"payments/{payment_id}")

    async def receivables(
        self,
        *,
        page: int = 1,
        per_page: int | None = None,
    ) -> Any:
        """Read the account-specific Facto collections resource.

        Facto's public Billing OpenAPI does not publish a list endpoint for
        Cobranza. Some accounts can receive an additional read-only resource
        from Facto support; its relative path is configured in Dokploy.
        """

        resource = self.settings.facto_receivables_resource.strip()
        if not resource:
            raise FactoError(
                "Facto no tiene configurado el recurso oficial de cobranza"
            )
        params: dict[str, Any] = {"page": max(1, page)}
        if per_page is not None:
            params["per_page"] = max(1, min(per_page, 100))
        return await self.get(resource, params=params)

    async def inbox_documents(
        self,
        *,
        page: int = 1,
        per_page: int | None = None,
    ) -> Any:
        """Read received-document metadata, including the supplier identity."""

        params: dict[str, Any] = {"page": max(1, page)}
        if per_page is not None:
            params["per_page"] = max(1, min(per_page, 100))
        return await self.get("inbox_documents", params=params)

    async def health(self) -> FactoHealth:
        if not self.configured:
            return FactoHealth(False, False, "Credenciales pendientes en Dokploy")
        try:
            await self.products(page=1)
        except FactoError as exc:
            return FactoHealth(True, False, str(exc))
        return FactoHealth(True, True, "Facto conectado en modo solo lectura")
