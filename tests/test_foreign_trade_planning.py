from datetime import date
from decimal import Decimal

from app.foreign_trade.planning import ForeignTradePlanner, InventoryPosition


def test_lead_time_is_95_days_without_february() -> None:
    planner = ForeignTradePlanner()
    assert planner.projected_arrival(date(2026, 3, 1)) == date(2026, 6, 4)


def test_february_factory_pause_delays_production() -> None:
    planner = ForeignTradePlanner()
    assert planner.projected_arrival(date(2026, 1, 15)) > date(2026, 4, 20)


def test_purchase_over_70000_is_blocked_without_auto_split() -> None:
    planner = ForeignTradePlanner()
    result = planner.recommend(
        InventoryPosition(
            sku="ST-TEST",
            available_units=0,
            committed_units=0,
            confirmed_inbound_units=0,
            average_daily_demand=Decimal("10"),
            unit_cost_usd=Decimal("100"),
        ),
        as_of=date(2026, 7, 27),
    )
    assert result.recommended_value_usd > Decimal("70000")
    assert result.purchase_policy == "blocked_over_70000"
    assert result.recommended_units > 0


def test_nearby_orders_cannot_bypass_limit() -> None:
    blocked, total, message = ForeignTradePlanner().detect_aggregate_limit(
        [Decimal("40000"), Decimal("35000")]
    )
    assert blocked is True
    assert total == Decimal("75000")
    assert "no dividir" in str(message)
