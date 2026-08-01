from datetime import date

import pytest

from app.foreign_trade.catalog import build_foreign_trade_report, load_import_catalog
from app.hub.agents.registry import AgentRegistry
from app.hub.contracts import AgentType, HubTask


def test_import_catalog_preserves_product_volume_evidence() -> None:
    catalog = load_import_catalog()

    assert catalog["coverage"]["products"] >= 180
    assert catalog["coverage"]["with_cbm"] >= 90
    assert any(
        item.get("unit_cbm") and item.get("source_document")
        for item in catalog["items"]
    )


def test_import_report_separates_recoverable_vat_from_landed_cost() -> None:
    report = build_foreign_trade_report(
        [
            {
                "sku": "ST-351",
                "name": "BRAND SUPER STARS ST-351",
                "available_units": 0,
                "average_daily_demand": 1,
            }
        ],
        as_of=date(2026, 7, 31),
    )
    proposal = report["purchase_proposal"]

    assert report["catalog"]["matched_inventory_products"] == 1
    assert proposal["items"]
    assert proposal["totals"]["recoverable_import_vat_cash_usd"] > 0
    assert proposal["totals"]["landed_cost_usd"] < (
        proposal["totals"]["landed_cost_usd"]
        + proposal["totals"]["recoverable_import_vat_cash_usd"]
    )
    assert proposal["totals"]["fob_usd"] <= 70000


def test_import_report_excludes_replenishment_without_volume_evidence() -> None:
    report = build_foreign_trade_report(
        [
            {
                "sku": "ST-428",
                "name": "BRAND SUPER STARS ST-428",
                "available_units": 0,
                "average_daily_demand": 1,
            }
        ],
        as_of=date(2026, 7, 31),
    )

    assert report["products"][0]["unit_cbm"] == 0
    assert report["purchase_proposal"]["items"] == []
    assert report["pending_volume_products"]
    assert "confirmar su m3 unitario" in " ".join(report["purchase_proposal"]["warnings"])


@pytest.mark.asyncio
async def test_foreign_trade_agent_builds_one_consolidated_review_proposal() -> None:
    result = await AgentRegistry().get(AgentType.foreign_trade).execute(
        HubTask(
            id="foreign-trade-plan",
            agent_type=AgentType.foreign_trade,
            action="review_import_plan",
            payload={
                "as_of": "2026-07-31",
                "products": [
                    {
                        "sku": "ST-351",
                        "name": "BRAND SUPER STARS ST-351",
                        "available_units": 0,
                        "average_daily_demand": 1,
                    }
                ],
            },
        )
    )

    assert result.metrics["matched_inventory_products"] == 1
    assert len(result.proposals) == 1
    assert result.proposals[0].requires_approval is True
    assert result.evidence[0]["foreign_trade_report"]["policy"]["lead_time_days"] == 95
    assert result.evidence[0]["foreign_trade_report"]["policy"]["target_coverage_days"] == 150
