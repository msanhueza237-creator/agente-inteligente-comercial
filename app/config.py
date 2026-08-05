from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "development"

    # Database (Supabase Postgres). database_url should use the Supavisor
    # pooled connection (port 6543) at runtime; migrations use the direct
    # connection (port 5432) via database_url_direct.
    database_url: str
    database_url_direct: str | None = None

    # Google Maps Places API
    google_maps_api_key: str | None = None
    google_places_daily_budget_usd: float = 20.0
    google_places_monthly_budget_usd: float = 400.0
    google_places_run_budget_usd: float = 10.0
    google_places_budget_alert_ratio: float = 0.70
    # A task may fan out into several complementary Text Search requests.
    # Place Details remains capped separately to keep the search predictable.
    google_places_queries_per_task: int = 6
    google_places_pages_per_query: int = 2
    google_places_detail_multiplier: int = 2

    # Licensed web search. The key is used only by the Brave API adapter.
    brave_search_api_key: str | None = None
    brave_market_queries_per_region: int = 8
    brave_search_cost_per_query_usd: float = 0.005
    brave_search_monthly_budget_usd: float = 5.0
    brave_search_free_credit_usd: float = 5.0

    # ERP and commerce integrations. Secrets remain in the Agent Hub service.
    facto_enabled: bool = False
    facto_api_base_url: str = "https://api-billing.koywe.com/V1"
    facto_client_id: SecretStr | None = None
    facto_client_secret: SecretStr | None = None
    facto_username: SecretStr | None = None
    facto_password: SecretStr | None = None
    # Facto's public Billing API does not currently document a collection
    # endpoint for Cobranza -> Documentos impagos.  Keep the account-specific
    # read-only resource configurable so support can enable it without a code
    # change. Example value (only when supplied by Facto): receivables
    facto_receivables_resource: str = ""
    facto_sync_interval_minutes: int = 30
    # Facto's document PDF can include a current "Saldo pendiente a pagar".
    # Refresh document details periodically so partial payments are not kept
    # forever in the in-memory cache.
    facto_document_detail_cache_minutes: int = 30
    facto_request_timeout_seconds: float = 30.0
    facto_read_only: bool = True

    tiendanube_enabled: bool = False
    tiendanube_api_base_url: str = "https://api.tiendanube.com/2025-03"
    tiendanube_store_id: str | None = None
    tiendanube_access_token: SecretStr | None = None
    tiendanube_user_agent: str = "ClimaActivaCRM/1.0 (msanhueza@latinchile.cl)"
    tiendanube_sync_interval_minutes: int = 15
    tiendanube_request_timeout_seconds: float = 30.0
    tiendanube_read_only: bool = True

    # CRM boundary. Production must use the restricted HTTP adapter; the fake
    # port is only allowed for development and tests.
    crm_mode: Literal["fake", "http"] = "fake"
    crm_base_url: str | None = None
    crm_api_key: SecretStr | None = None
    crm_worker_id: str = "climactiva-worker-01"
    crm_timeout_seconds: float = 15.0

    # Compatibility flags only. The connector is hard-disabled in code until
    # an authorized official API/feed implementation replaces the placeholder.
    paginas_amarillas_enabled: bool = False
    paginas_amarillas_license_confirmed: bool = False

    # Dashboard auth
    session_secret_key: str = "change-me-in-production"

    # Scheduler
    dedup_fuzzy_auto_merge_threshold: float = 90.0
    dedup_fuzzy_review_threshold: float = 75.0
    region_category_recheck_days: int = 30
    worker_poll_seconds: int = 15
    worker_lease_seconds: int = 120
    worker_heartbeat_seconds: int = 30
    worker_task_max_attempts: int = 3
    website_max_bytes: int = 1_500_000
    website_timeout_seconds: float = 10.0

    # Multi-agent hub scheduler.
    hub_worker_id: str = "climactiva-hub-01"
    # Dokploy normally starts the Dockerfile web command only. Keep the Hub
    # consumer embedded so CRM tasks cannot remain pending silently.
    hub_embedded_worker: bool = True
    hub_poll_seconds: int = 15
    hub_lease_seconds: int = 120
    hub_heartbeat_seconds: int = 30
    # Re-run the customer x product radar periodically. It only creates
    # reviewable CRM proposals; it never sends a campaign automatically.
    hub_commercial_auto_analysis_enabled: bool = True
    hub_commercial_auto_analysis_interval_minutes: int = 360

    @model_validator(mode="after")
    def production_crm_contract(self) -> "Settings":
        if self.env == "production" and self.crm_mode != "http":
            raise ValueError("CRM_MODE=http is mandatory in production")
        if self.crm_mode == "http":
            if not self.crm_base_url or not self.crm_base_url.startswith("https://"):
                raise ValueError("CRM_BASE_URL must use HTTPS when CRM_MODE=http")
            if self.crm_api_key is None or not self.crm_api_key.get_secret_value().strip():
                raise ValueError("CRM_API_KEY is required when CRM_MODE=http")
        if not self.crm_worker_id.strip():
            raise ValueError("CRM_WORKER_ID cannot be empty")
        if self.facto_enabled:
            facto_secrets = (
                self.facto_client_id,
                self.facto_client_secret,
                self.facto_username,
                self.facto_password,
            )
            if any(value is None or not value.get_secret_value().strip() for value in facto_secrets):
                raise ValueError("Facto credentials are required when FACTO_ENABLED=true")
            if not self.facto_api_base_url.startswith("https://"):
                raise ValueError("FACTO_API_BASE_URL must use HTTPS")
            receivables_resource = self.facto_receivables_resource.strip()
            if (
                "://" in receivables_resource
                or ".." in receivables_resource
                or receivables_resource.startswith("/")
            ):
                raise ValueError(
                    "FACTO_RECEIVABLES_RESOURCE must be a relative API resource"
                )
        if self.tiendanube_enabled:
            if not self.tiendanube_store_id or not self.tiendanube_store_id.strip():
                raise ValueError("TIENDANUBE_STORE_ID is required when TIENDANUBE_ENABLED=true")
            if (
                self.tiendanube_access_token is None
                or not self.tiendanube_access_token.get_secret_value().strip()
            ):
                raise ValueError(
                    "TIENDANUBE_ACCESS_TOKEN is required when TIENDANUBE_ENABLED=true"
                )
            if not self.tiendanube_api_base_url.startswith("https://"):
                raise ValueError("TIENDANUBE_API_BASE_URL must use HTTPS")
        if min(
            self.facto_sync_interval_minutes,
            self.facto_document_detail_cache_minutes,
            self.tiendanube_sync_interval_minutes,
            self.hub_poll_seconds,
            self.hub_lease_seconds,
            self.hub_heartbeat_seconds,
            self.hub_commercial_auto_analysis_interval_minutes,
        ) <= 0:
            raise ValueError("Integration and Agent Hub intervals must be positive")
        if self.hub_heartbeat_seconds >= self.hub_lease_seconds:
            raise ValueError("HUB_HEARTBEAT_SECONDS must be lower than HUB_LEASE_SECONDS")
        if not self.hub_worker_id.strip():
            raise ValueError("HUB_WORKER_ID cannot be empty")
        if not 1 <= self.google_places_queries_per_task <= 12:
            raise ValueError("GOOGLE_PLACES_QUERIES_PER_TASK must be between 1 and 12")
        if not 1 <= self.google_places_pages_per_query <= 3:
            raise ValueError("GOOGLE_PLACES_PAGES_PER_QUERY must be between 1 and 3")
        if not 1 <= self.google_places_detail_multiplier <= 3:
            raise ValueError("GOOGLE_PLACES_DETAIL_MULTIPLIER must be between 1 and 3")
        if not 3 <= self.brave_market_queries_per_region <= 12:
            raise ValueError("BRAVE_MARKET_QUERIES_PER_REGION must be between 3 and 12")
        if min(self.brave_search_cost_per_query_usd, self.brave_search_monthly_budget_usd) <= 0:
            raise ValueError("Brave Search cost and monthly budget must be positive")
        if self.brave_search_free_credit_usd < 0:
            raise ValueError("BRAVE_SEARCH_FREE_CREDIT_USD cannot be negative")
        if (
            min(
                self.google_places_run_budget_usd,
                self.google_places_daily_budget_usd,
                self.google_places_monthly_budget_usd,
            )
            <= 0
        ):
            raise ValueError("Google Places budgets must be positive")
        if not 0.1 <= self.google_places_budget_alert_ratio <= 1:
            raise ValueError("GOOGLE_PLACES_BUDGET_ALERT_RATIO must be between 0.1 and 1")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
