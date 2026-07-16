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
    google_places_detail_multiplier: int = 2

    # Licensed web search. The key is used only by the Brave API adapter.
    brave_search_api_key: str | None = None
    brave_market_queries_per_region: int = 8

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
        if not 1 <= self.google_places_queries_per_task <= 12:
            raise ValueError("GOOGLE_PLACES_QUERIES_PER_TASK must be between 1 and 12")
        if not 1 <= self.google_places_detail_multiplier <= 3:
            raise ValueError("GOOGLE_PLACES_DETAIL_MULTIPLIER must be between 1 and 3")
        if not 3 <= self.brave_market_queries_per_region <= 12:
            raise ValueError("BRAVE_MARKET_QUERIES_PER_REGION must be between 3 and 12")
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
