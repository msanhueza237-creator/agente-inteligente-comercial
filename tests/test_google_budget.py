from app.config import Settings
from app.db.models import PlacesFieldTier, PlacesQueryType
from app.prospecting.budget import MemoryGooglePlacesBudget, estimate_google_run
from app.prospecting.contracts import (
    ProspectingCampaign,
    ProspectingRunSnapshot,
    SourceName,
    Territory,
)


def settings(**updates) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/test",
        **updates,
    )


async def reserve(budget, run_id="run-budget"):
    return await budget.reserve(
        run_id=run_id,
        task_id="task-budget",
        query_type=PlacesQueryType.text_search,
        tier=PlacesFieldTier.pro,
        region="Metropolitana de Santiago",
        keyword="climatizacion",
    )


async def test_run_budget_stops_new_google_calls_and_raises_alert() -> None:
    budget = MemoryGooglePlacesBudget(
        settings(
            google_places_run_budget_usd=0.05,
            google_places_daily_budget_usd=1,
            google_places_monthly_budget_usd=2,
            google_places_budget_alert_ratio=0.5,
        )
    )

    first = await reserve(budget)
    second = await reserve(budget)

    assert first.allowed
    assert first.alert
    assert not second.allowed
    assert second.reason == "run_budget_exhausted"
    assert second.run_spend_usd == 0.032


async def test_daily_budget_is_shared_between_runs() -> None:
    budget = MemoryGooglePlacesBudget(
        settings(
            google_places_run_budget_usd=1,
            google_places_daily_budget_usd=0.05,
            google_places_monthly_budget_usd=2,
        )
    )

    assert (await reserve(budget, "run-a")).allowed
    denied = await reserve(budget, "run-b")

    assert not denied.allowed
    assert denied.reason == "daily_budget_exhausted"


def test_estimate_for_one_hundred_candidate_capacity() -> None:
    snapshot = ProspectingRunSnapshot(
        crm_run_id="run-estimate",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign-estimate",
            name="100 Santiago",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13101",
                    comuna_name="Santiago",
                ),
            ),
            keywords=("climatizacion", "aire acondicionado", "refrigeracion", "hvac", "frio"),
            sources=(SourceName.google_places,),
            max_results_per_task=20,
            max_candidates=100,
        ),
    )

    estimate = estimate_google_run(snapshot, settings())

    assert estimate == {
        "google_tasks": 5,
        "estimated_min_cost_usd": 0.96,
        "estimated_cost_usd": 2.96,
        "estimated_max_cost_usd": 4.96,
    }
