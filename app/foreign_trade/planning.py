from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
from typing import Iterable


@dataclass(frozen=True)
class LeadTimePolicy:
    production_days: int = 45
    sea_travel_days: int = 45
    customs_delay_days: int = 5
    safety_stock_days: int = 30
    review_period_days: int = 30
    target_coverage_days: int = 150
    factory_shutdown_months: tuple[int, ...] = (2,)


@dataclass(frozen=True)
class InventoryPosition:
    sku: str
    available_units: int
    committed_units: int
    confirmed_inbound_units: int
    average_daily_demand: Decimal
    unit_cost_usd: Decimal


@dataclass(frozen=True)
class ReplenishmentRecommendation:
    sku: str
    net_units: int
    reorder_point_units: int
    target_units: int
    recommended_units: int
    recommended_value_usd: Decimal
    projected_stockout_date: date | None
    required_order_date: date | None
    severity: str
    purchase_policy: str
    warnings: tuple[str, ...]


class ForeignTradePlanner:
    """Deterministic purchase planner. It never creates or splits purchase orders."""

    high_season_months = frozenset({11, 12, 1, 2})
    target_po_min_usd = Decimal("50000")
    hard_po_max_usd = Decimal("70000")

    def __init__(self, policy: LeadTimePolicy | None = None) -> None:
        self.policy = policy or LeadTimePolicy()

    def projected_arrival(self, order_date: date) -> date:
        production_end = self._add_factory_days(order_date, self.policy.production_days)
        return (
            production_end
            + timedelta(days=self.policy.sea_travel_days)
            + timedelta(days=self.policy.customs_delay_days)
        )

    def required_order_date(self, desired_arrival: date) -> date:
        travel_and_customs = self.policy.sea_travel_days + self.policy.customs_delay_days
        production_end = desired_arrival - timedelta(days=travel_and_customs)
        current = production_end
        remaining = self.policy.production_days
        while remaining > 0:
            current -= timedelta(days=1)
            if current.month not in self.policy.factory_shutdown_months:
                remaining -= 1
        return current

    def _add_factory_days(self, start: date, days: int) -> date:
        current = start
        remaining = days
        while remaining > 0:
            current += timedelta(days=1)
            if current.month not in self.policy.factory_shutdown_months:
                remaining -= 1
        return current

    @staticmethod
    def _ceil_units(value: Decimal) -> int:
        return int(value.quantize(Decimal("1"), rounding=ROUND_CEILING))

    def recommend(
        self,
        position: InventoryPosition,
        *,
        as_of: date,
        demand_multiplier: Decimal = Decimal("1"),
    ) -> ReplenishmentRecommendation:
        adjusted_demand = max(Decimal("0"), position.average_daily_demand * demand_multiplier)
        net = (
            position.available_units
            - position.committed_units
            + position.confirmed_inbound_units
        )
        reorder_days = (
            self.policy.production_days
            + self.policy.sea_travel_days
            + self.policy.customs_delay_days
            + self.policy.safety_stock_days
        )
        reorder_point = self._ceil_units(adjusted_demand * reorder_days)
        target = self._ceil_units(adjusted_demand * self.policy.target_coverage_days)
        quantity = max(0, target - net)
        value = (Decimal(quantity) * position.unit_cost_usd).quantize(Decimal("0.01"))
        stockout = (
            as_of + timedelta(days=max(0, int(net / adjusted_demand)))
            if adjusted_demand > 0
            else None
        )
        order_date = self.required_order_date(stockout) if stockout else None
        warnings: list[str] = []
        if as_of.month in self.high_season_months:
            warnings.append("Demanda en temporada alta noviembre-febrero")
        if self.projected_arrival(as_of).month in self.policy.factory_shutdown_months:
            warnings.append("La produccion atraviesa la pausa de fabrica de febrero")
        if value > self.hard_po_max_usd:
            policy = "blocked_over_70000"
            warnings.append("Supera USD 70.000: requiere ajustar cantidades con aprobacion humana")
        elif value >= self.target_po_min_usd:
            policy = "target_range_50000_70000"
        elif quantity > 0:
            policy = "below_target_requires_reason"
            warnings.append("Orden bajo USD 50.000: registrar justificacion comercial")
        else:
            policy = "no_purchase"
        if net <= 0:
            severity = "critical"
        elif net <= reorder_point:
            severity = "high"
        elif net <= target:
            severity = "medium"
        else:
            severity = "low"
        return ReplenishmentRecommendation(
            sku=position.sku,
            net_units=net,
            reorder_point_units=reorder_point,
            target_units=target,
            recommended_units=quantity,
            recommended_value_usd=value,
            projected_stockout_date=stockout,
            required_order_date=order_date,
            severity=severity,
            purchase_policy=policy,
            warnings=tuple(warnings),
        )

    def detect_aggregate_limit(
        self, proposed_values: Iterable[Decimal], *, window_days: int = 30
    ) -> tuple[bool, Decimal, str | None]:
        total = sum(proposed_values, Decimal("0"))
        if total > self.hard_po_max_usd:
            return (
                True,
                total,
                f"Ordenes cercanas en {window_days} dias suman mas de USD 70.000; "
                "no dividir para evadir el limite",
            )
        return False, total, None
