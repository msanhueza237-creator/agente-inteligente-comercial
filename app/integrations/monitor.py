from __future__ import annotations

import logging

from app.config import Settings
from app.crm.http import HttpCRMPort
from app.enrichment.google_places import GooglePlacesClient

logger = logging.getLogger("clima_activa.integrations")


class IntegrationMonitor:
    def __init__(self, crm: HttpCRMPort, settings: Settings) -> None:
        self.crm = crm
        self.settings = settings

    async def poll_once(self) -> bool:
        check = await self.crm.claim_integration_check(self.settings.crm_worker_id)
        if not check:
            return False

        check_id = str(check.get("id", ""))
        provider = str(check.get("provider", ""))
        if provider == "google_places":
            await self._check_google_places(check_id)
        elif provider == "brave_search":
            await self._report_not_configured(
                check_id,
                provider,
                bool(self.settings.brave_search_api_key),
            )
        else:
            await self.crm.report_integration_status(
                worker_id=self.settings.crm_worker_id,
                check_id=check_id,
                provider=provider,
                configured=False,
                status="error",
                error_code="unsupported_provider",
                message="El agente no reconoce este proveedor.",
            )
        return True

    async def _check_google_places(self, check_id: str) -> None:
        configured = bool(self.settings.google_maps_api_key)
        if not configured:
            await self._report_not_configured(check_id, "google_places", False)
            return

        result = await GooglePlacesClient(self.settings.google_maps_api_key).check_connection()
        await self.crm.report_integration_status(
            worker_id=self.settings.crm_worker_id,
            check_id=check_id,
            provider="google_places",
            configured=True,
            status=str(result["status"]),
            error_code=(str(result["error_code"]) if result.get("error_code") else None),
            message=str(result["message"]),
            metadata={
                "daily_budget_usd": self.settings.google_places_daily_budget_usd,
                "monthly_budget_usd": self.settings.google_places_monthly_budget_usd,
            },
        )

    async def _report_not_configured(
        self, check_id: str, provider: str, configured: bool
    ) -> None:
        await self.crm.report_integration_status(
            worker_id=self.settings.crm_worker_id,
            check_id=check_id,
            provider=provider,
            configured=configured,
            status="not_configured" if not configured else "pending",
            error_code="missing_api_key" if not configured else None,
            message=(
                "La clave del proveedor no esta configurada en el agente."
                if not configured
                else "La prueba de este proveedor todavia no esta implementada."
            ),
        )
