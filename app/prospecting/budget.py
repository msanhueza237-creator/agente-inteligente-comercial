from __future__ import annotations

import uuid
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import (
    GoogleMapsQueryLog,
    BraveSearchQueryLog,
    PlacesFieldTier,
    PlacesQueryType,
)
from app.enrichment.google_places import COST_ESTIMATE_USD
from app.prospecting.contracts import ProspectingRunSnapshot, SourceName


@dataclass(frozen=True)
class BudgetReservation:
    allowed: bool
    cost_usd: float
    run_spend_usd: float
    daily_spend_usd: float
    monthly_spend_usd: float
    alert: bool = False
    reason: str | None = None
    log_id: str | None = None


class GooglePlacesBudget(Protocol):
    async def reserve(
        self,
        *,
        run_id: str,
        task_id: str,
        query_type: PlacesQueryType,
        tier: PlacesFieldTier,
        region: str,
        keyword: str,
    ) -> BudgetReservation: ...

    async def complete(self, reservation: BudgetReservation, results_count: int) -> None: ...


def estimate_google_run(
    snapshot: ProspectingRunSnapshot, settings: Settings
) -> dict[str, float | int]:
    if SourceName.google_places not in snapshot.campaign.sources:
        return {
            "google_tasks": 0,
            "estimated_min_cost_usd": 0.0,
            "estimated_cost_usd": 0.0,
            "estimated_max_cost_usd": 0.0,
        }
    tasks = len(snapshot.campaign.territories) * len(snapshot.campaign.keywords)
    search_cost = tasks * settings.google_places_queries_per_task * COST_ESTIMATE_USD["pro"]
    expected_details = tasks * snapshot.campaign.max_results_per_task
    maximum_details = expected_details * settings.google_places_detail_multiplier
    expected = search_cost + expected_details * COST_ESTIMATE_USD["enterprise"]
    maximum = search_cost + maximum_details * COST_ESTIMATE_USD["enterprise"]
    return {
        "google_tasks": tasks,
        "estimated_min_cost_usd": round(search_cost, 4),
        "estimated_cost_usd": round(min(expected, settings.google_places_run_budget_usd), 4),
        "estimated_max_cost_usd": round(min(maximum, settings.google_places_run_budget_usd), 4),
    }


def _decision(
    settings: Settings,
    *,
    cost: float,
    run_spend: float,
    daily_spend: float,
    monthly_spend: float,
) -> BudgetReservation:
    projected_run = run_spend + cost
    projected_day = daily_spend + cost
    projected_month = monthly_spend + cost
    reason = None
    if projected_run > settings.google_places_run_budget_usd:
        reason = "run_budget_exhausted"
    elif projected_day > settings.google_places_daily_budget_usd:
        reason = "daily_budget_exhausted"
    elif projected_month > settings.google_places_monthly_budget_usd:
        reason = "monthly_budget_exhausted"
    ratios = (
        projected_run / settings.google_places_run_budget_usd,
        projected_day / settings.google_places_daily_budget_usd,
        projected_month / settings.google_places_monthly_budget_usd,
    )
    return BudgetReservation(
        allowed=reason is None,
        cost_usd=cost,
        run_spend_usd=projected_run if reason is None else run_spend,
        daily_spend_usd=projected_day if reason is None else daily_spend,
        monthly_spend_usd=projected_month if reason is None else monthly_spend,
        alert=reason is None and max(ratios) >= settings.google_places_budget_alert_ratio,
        reason=reason,
    )


class MemoryGooglePlacesBudget:
    """Test/development ledger. Production uses the persistent implementation."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._run_spend: dict[str, float] = {}
        self._daily_spend = 0.0
        self._monthly_spend = 0.0
        self._day = datetime.now(timezone.utc).date()
        self._month = (self._day.year, self._day.month)

    def _roll_periods(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._daily_spend = 0.0
            self._day = today
        month = (today.year, today.month)
        if month != self._month:
            self._monthly_spend = 0.0
            self._month = month

    async def reserve(
        self,
        *,
        run_id: str,
        task_id: str,
        query_type: PlacesQueryType,
        tier: PlacesFieldTier,
        region: str,
        keyword: str,
    ) -> BudgetReservation:
        del task_id, query_type, region, keyword
        self._roll_periods()
        cost = COST_ESTIMATE_USD[tier.value]
        decision = _decision(
            self.settings,
            cost=cost,
            run_spend=self._run_spend.get(run_id, 0.0),
            daily_spend=self._daily_spend,
            monthly_spend=self._monthly_spend,
        )
        if decision.allowed:
            self._run_spend[run_id] = decision.run_spend_usd
            self._daily_spend = decision.daily_spend_usd
            self._monthly_spend = decision.monthly_spend_usd
        return decision

    async def complete(self, reservation: BudgetReservation, results_count: int) -> None:
        del reservation, results_count


class PersistentGooglePlacesBudget:
    """Durable budget reservations backed by the existing Google query log."""

    _ADVISORY_LOCK_KEY = 1729042201

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.sessions = sessions

    async def _spend(
        self, session: AsyncSession, *, since: datetime, run_id: str | None = None
    ) -> float:
        statement = select(func.coalesce(func.sum(GoogleMapsQueryLog.cost_estimate_usd), 0)).where(
            GoogleMapsQueryLog.created_at >= since
        )
        if run_id is not None:
            statement = statement.where(
                GoogleMapsQueryLog.query_params["crm_run_id"].as_string() == run_id
            )
        return float((await session.execute(statement)).scalar_one())

    async def reserve(
        self,
        *,
        run_id: str,
        task_id: str,
        query_type: PlacesQueryType,
        tier: PlacesFieldTier,
        region: str,
        keyword: str,
    ) -> BudgetReservation:
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)
        async with self.sessions() as session, session.begin():
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": self._ADVISORY_LOCK_KEY},
                )
            run_spend = await self._spend(
                session, since=datetime.min.replace(tzinfo=timezone.utc), run_id=run_id
            )
            daily_spend = await self._spend(session, since=day_start)
            monthly_spend = await self._spend(session, since=month_start)
            cost = COST_ESTIMATE_USD[tier.value]
            decision = _decision(
                self.settings,
                cost=cost,
                run_spend=run_spend,
                daily_spend=daily_spend,
                monthly_spend=monthly_spend,
            )
            if not decision.allowed:
                return decision
            log = GoogleMapsQueryLog(
                query_type=query_type,
                query_params={"crm_run_id": run_id, "task_id": task_id},
                region=region,
                category=keyword,
                results_count=None,
                field_mask_tier=tier,
                cost_estimate_usd=cost,
            )
            session.add(log)
            await session.flush()
            return BudgetReservation(**{**decision.__dict__, "log_id": str(log.id)})

    async def complete(self, reservation: BudgetReservation, results_count: int) -> None:
        if not reservation.log_id:
            return
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(GoogleMapsQueryLog)
                .where(GoogleMapsQueryLog.id == uuid.UUID(reservation.log_id))
                .values(results_count=results_count)
            )


@dataclass(frozen=True)
class BraveBudgetReservation:
    allowed: bool
    monthly_queries: int
    monthly_spend_usd: float
    monthly_limit_usd: float
    remaining_usd: float
    reason: str | None = None
    log_id: str | None = None


class PersistentBraveSearchBudget:
    """Atomically reserves each Brave request before sending it."""

    _ADVISORY_LOCK_KEY = 1729042202

    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.settings = settings
        self.sessions = sessions

    async def reserve(self, *, run_id: str, task_id: str, query: str, monthly_limit_usd: float,
                      query_kind: str = "discovery", max_social_queries: int = 0) -> BraveBudgetReservation:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        cost = self.settings.brave_search_cost_per_query_usd
        async with self.sessions() as session, session.begin():
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": self._ADVISORY_LOCK_KEY})
            statement = select(
                func.count(BraveSearchQueryLog.id),
                func.coalesce(func.sum(BraveSearchQueryLog.cost_estimate_usd), 0),
            ).where(BraveSearchQueryLog.created_at >= month_start)
            count, spend = (await session.execute(statement)).one()
            spend = float(spend)
            if query_kind == "social":
                social_count = int((await session.execute(select(func.count(BraveSearchQueryLog.id)).where(
                    BraveSearchQueryLog.crm_run_id == run_id,
                    BraveSearchQueryLog.query_kind == "social",
                ))).scalar_one())
                if social_count >= max_social_queries:
                    return BraveBudgetReservation(False, int(count), spend, monthly_limit_usd,
                                                    max(0.0, monthly_limit_usd - spend), "social_query_limit_exhausted")
            if spend + cost > monthly_limit_usd:
                return BraveBudgetReservation(False, int(count), spend, monthly_limit_usd,
                                                max(0.0, monthly_limit_usd - spend), "monthly_budget_exhausted")
            log = BraveSearchQueryLog(
                crm_run_id=run_id, task_id=task_id, query_kind=query_kind,
                query_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(),
                cost_estimate_usd=cost,
            )
            session.add(log)
            await session.flush()
            projected = spend + cost
            return BraveBudgetReservation(True, int(count) + 1, projected, monthly_limit_usd,
                                            max(0.0, monthly_limit_usd - projected), log_id=str(log.id))

    async def complete(self, reservation: BraveBudgetReservation, results_count: int) -> None:
        if not reservation.log_id:
            return
        async with self.sessions() as session, session.begin():
            await session.execute(update(BraveSearchQueryLog).where(
                BraveSearchQueryLog.id == uuid.UUID(reservation.log_id)).values(results_count=results_count))

    async def monthly_summary(self, monthly_limit_usd: float | None = None) -> dict[str, float | int]:
        limit = monthly_limit_usd or self.settings.brave_search_monthly_budget_usd
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with self.sessions() as session:
            count, spend = (await session.execute(select(
                func.count(BraveSearchQueryLog.id),
                func.coalesce(func.sum(BraveSearchQueryLog.cost_estimate_usd), 0),
            ).where(BraveSearchQueryLog.created_at >= month_start))).one()
        spend = float(spend)
        return {"monthly_queries": int(count), "monthly_spend_usd": round(spend, 4),
                "monthly_limit_usd": limit, "remaining_usd": round(max(0.0, limit - spend), 4),
                "free_credit_usd": self.settings.brave_search_free_credit_usd}
