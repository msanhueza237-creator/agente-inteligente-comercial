from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.crm.http import HttpCRMPort
from app.enrichment.google_places import GooglePlacesClient
from app.prospecting.budget import PersistentBraveSearchBudget, brave_provider_usage_from_headers

logger = logging.getLogger("clima_activa.integrations")


class IntegrationMonitor:
    def __init__(self, crm: HttpCRMPort, settings: Settings, brave_budget: PersistentBraveSearchBudget | None = None) -> None:
        self.crm = crm
        self.settings = settings
        self.brave_budget = brave_budget

    async def poll_once(self) -> bool:
        check = await self.crm.claim_integration_check(self.settings.crm_worker_id)
        if not check:
            return False

        check_id = str(check.get("id", ""))
        provider = str(check.get("provider", ""))
        if provider == "google_places":
            await self._check_google_places(check_id)
        elif provider == "brave_search":
            await self._check_brave_search(check_id)
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
                "run_budget_usd": self.settings.google_places_run_budget_usd,
                "budget_alert_percent": int(self.settings.google_places_budget_alert_ratio * 100),
            },
        )

    async def _check_brave_search(self, check_id: str) -> None:
        """Verify Brave with one minimal query and never retain its payload."""
        api_key = self.settings.brave_search_api_key
        if not api_key:
            await self._report_not_configured(check_id, "brave_search", False)
            return

        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                    params={"q": "climatizacion Chile", "count": 1, "country": "CL"},
                )
        except httpx.TimeoutException:
            result = ("error", "timeout", "Brave Search no respondio dentro del tiempo esperado.")
        except httpx.TransportError:
            result = (
                "error",
                "network_error",
                "No fue posible conectar con Brave Search desde el agente.",
            )
        else:
            status_code = response.status_code
            if status_code == 200:
                result = ("connected", None, "Brave Search respondio correctamente.")
            elif status_code == 429:
                result = (
                    "quota_exhausted",
                    "quota_exhausted",
                    "Brave Search rechazo la prueba por cuota o limite de consumo.",
                )
            elif status_code in {401, 403}:
                result = (
                    "error",
                    "forbidden",
                    "Brave Search rechazo la credencial. Revisa la suscripcion y restricciones de la clave.",
                )
            elif status_code >= 500:
                result = (
                    "error",
                    "provider_unavailable",
                    "Brave Search esta temporalmente no disponible.",
                )
            else:
                result = (
                    "error",
                    f"http_{status_code}",
                    "Brave Search rechazo la solicitud de prueba.",
                )

        status, error_code, message = result
        metadata = {
            "cost_per_query_usd": getattr(self.settings, "brave_search_cost_per_query_usd", 0.005),
            "monthly_limit_usd": getattr(self.settings, "brave_search_monthly_budget_usd", 5.0),
            "free_credit_usd": getattr(self.settings, "brave_search_free_credit_usd", 5.0),
        }
        if self.brave_budget:
            response_headers = getattr(locals().get("response"), "headers", {})
            provider_usage = brave_provider_usage_from_headers(response_headers)
            if status == "connected" and provider_usage:
                provider_queries, provider_limit, reset_seconds = provider_usage
                metadata.update(await self.brave_budget.reconcile_provider_usage(
                    provider_queries=provider_queries,
                    provider_limit_queries=provider_limit,
                    provider_remaining_queries=max(0, provider_limit - provider_queries),
                    reset_seconds=reset_seconds,
                ))
                metadata["usage_source"] = "brave_rate_limit_headers"
            else:
                metadata.update(await self.brave_budget.monthly_summary())
                metadata["usage_source"] = "local_ledger"
        await self.crm.report_integration_status(
            worker_id=self.settings.crm_worker_id,
            check_id=check_id,
            provider="brave_search",
            configured=True,
            status=status,
            error_code=error_code,
            message=message,
            metadata=metadata,
        )

    async def _report_not_configured(self, check_id: str, provider: str, configured: bool) -> None:
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
