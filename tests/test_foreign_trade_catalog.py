from datetime import date

import pytest

from app.foreign_trade.catalog import (
    build_foreign_trade_report,
    load_active_imports,
    load_freight_history,
    load_import_catalog,
    resolve_customs_cost_references,
    resolve_freight_history,
)
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


def test_freight_reference_uses_latest_verified_ads_20gp_invoice() -> None:
    freight = load_freight_history()

    assert freight["provider"]["name"] == "ADS INTERNACIONAL CARGO SPA"
    assert freight["container_policy"]["type"] == "20GP"
    assert freight["container_policy"]["planning_capacity_cbm"] == pytest.approx(27)
    assert freight["summary"]["latest_invoice_number"] == "1702"
    assert freight["summary"]["latest_verified_usd"] == pytest.approx(3400)


def test_freight_reference_prefers_ad_cargas_invoice_synced_from_crm() -> None:
    freight = resolve_freight_history(
        [
            {
                "issuer_legal_name": "AD CARGAS INTERNACIONAL SPA",
                "folio": "2044",
                "issue_date": "2026-07-25",
                "currency_code": "USD",
                "net_amount": 4100,
                "description": "Flete internacional maritimo 20GP Ningbo - San Antonio",
                "crm_external_id": "facto-purchase-2044",
                "crm_resource": "purchase_document_details",
                "crm_updated_at": "2026-07-26T10:30:00Z",
            }
        ]
    )

    summary = freight["summary"]
    assert summary["latest_invoice_number"] == "2044"
    assert summary["latest_invoice_date"] == "2026-07-25"
    assert summary["latest_verified_usd"] == pytest.approx(4100)
    assert summary["latest_source"] == "crm_facto_purchase_invoice"
    assert summary["fallback_used"] is False
    assert summary["crm_invoice_candidates"] == 1
    assert summary["crm_usable_invoices"] == 1
    assert freight["crm_facto_candidates"][0]["source"]["provider"] == "facto"


def test_freight_reference_keeps_auditable_fallback_for_unusable_crm_invoice() -> None:
    freight = resolve_freight_history(
        [
            {
                "issuer_name": "ADS Internacional Cargo SpA",
                "folio": "2050",
                "issue_date": "2026-07-30",
                "currency_code": "CLP",
                "net_amount": 3_800_000,
            }
        ]
    )

    summary = freight["summary"]
    assert summary["latest_invoice_number"] == "1702"
    assert summary["latest_verified_usd"] == pytest.approx(3400)
    assert summary["fallback_used"] is True
    assert summary["crm_invoice_candidates"] == 1
    assert summary["crm_usable_invoices"] == 0


def test_import_report_values_freight_with_crm_facto_invoice() -> None:
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
        freight_invoices=[
            {
                "issuer_legal_name": "AD CARGAS INTERNACIONAL SPA",
                "folio": "2044",
                "issue_date": "2026-07-25",
                "currency": "USD",
                "net_amount": 4100,
            }
        ],
    )

    proposal = report["purchase_proposal"]
    assert proposal["totals"]["freight_usd"] == pytest.approx(
        4100 * proposal["totals"]["total_cbm"] / 27,
        abs=0.02,
    )
    assert proposal["freight_reference"]["latest_invoice_number"] == "2044"
    assert proposal["freight_reference"]["latest_source"] == "crm_facto_purchase_invoice"


def test_customs_cost_references_use_agency_domain_and_are_never_fixed_tariffs() -> None:
    references = resolve_customs_cost_references(
        [
            {
                "message_id": "gmail-new",
                "from": "operaciones@agenciarodriguezpalma.cl",
                "subject": "Solicitud de fondos despacho 52000",
                "date": "2026-08-01",
                "attachment_name": "Solicitud fondos 52000.pdf",
                "dispatch": "52000",
            },
            {
                "message_id": "ignored",
                "from": "otro@proveedor.cl",
                "subject": "Tarifa fija",
            },
        ]
    )

    assert references["summary"]["latest_dispatch"] == "52000"
    assert references["summary"]["reference_contact"] == "j.rodriguez@agenciarodriguezpalma.cl"
    assert references["summary"]["fixed_tariff"] is False
    assert references["summary"]["costs_are_variable"] is True
    assert all(row["message_id"] != "ignored" for row in references["verified_email_documents"])


def test_customs_cost_references_keep_all_gmail_attachment_names() -> None:
    references = resolve_customs_cost_references(
        [
            {
                "gmail_message_id": "gmail-attachments",
                "from": "contable@agenciarodriguezpalma.cl",
                "subject": "Factura y cuenta corriente despacho 51590",
                "date": "2026-07-31T15:20:00Z",
                "attachment_names": ["FACTURA 26286.pdf", "CTA CTE 51590.pdf"],
            }
        ]
    )

    document = next(
        row
        for row in references["verified_email_documents"]
        if row["message_id"] == "gmail-attachments"
    )
    assert document["attachments"] == ["FACTURA 26286.pdf", "CTA CTE 51590.pdf"]


def test_active_import_tracks_full_proforma_and_timeline() -> None:
    payload = load_active_imports()
    active = payload["imports"][0]

    assert active["order_number"] == "26TDC12"
    assert len(active["items"]) == 89
    assert active["reconciliation"]["numbered_item_rows"] == 88
    assert active["reconciliation"]["unnumbered_item_rows"] == 1
    assert active["reconciliation"]["matches_document_total"] is True
    assert active["totals"]["fob_usd"] == pytest.approx(69452.33)
    assert active["totals"]["cartons"] == pytest.approx(539.70)
    assert active["totals"]["gross_weight_kg"] == pytest.approx(5599.55)
    assert active["totals"]["total_cbm"] == pytest.approx(26.84)
    assert active["source"]["sha256"] == "41bf12aee012431f77de33213a6c8cc97f69bd5167267664cd6370d8c19b2c05"
    assert any(
        item["name"] == "LI-BATTERY TUBE BENDER"
        and item["source_line_number"] is None
        and item["total_fob_usd"] == pytest.approx(1648.85)
        for item in active["items"]
    )
    assert active["timeline"]["production_end_date"] == "2026-09-11"
    assert active["timeline"]["estimated_warehouse_date"] == "2026-10-31"


def test_active_import_is_confirmed_inbound_not_available_stock() -> None:
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
    product = report["products"][0]

    assert product["available_units"] == 0
    assert product["active_import_inbound_units"] == 108
    assert product["confirmed_inbound_units"] == 108
    assert product["recommended_units"] < 150
    assert report["active_imports"][0]["status"] == "in_production"
    assert report["active_imports"][0]["estimated_costs"]["landed_cost_usd"] > 69452.33


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
    assert proposal["container_type"] == "20GP"
    assert proposal["container_reference_cbm"] == pytest.approx(27)
    assert proposal["totals"]["freight_usd"] == pytest.approx(
        3400 * proposal["totals"]["total_cbm"] / 27,
        abs=0.02,
    )
    assert proposal["freight_full_container_usd"] == pytest.approx(3400)
    assert proposal["freight_allocation_policy"] == "proportional_to_used_cbm"
    assert proposal["freight_proration_factor"] == pytest.approx(
        proposal["totals"]["total_cbm"] / 27,
        abs=0.0002,
    )
    assert sum(item["costs"]["freight_usd"] for item in proposal["items"]) == pytest.approx(
        proposal["totals"]["freight_usd"],
        abs=0.02,
    )
    assert proposal["container_remaining_cbm"] == pytest.approx(
        27 - proposal["totals"]["total_cbm"], abs=0.02
    )


def test_purchase_proposal_uses_complete_packing_boxes() -> None:
    report = build_foreign_trade_report(
        [
            {
                "sku": "ST-351",
                "name": "BRAND SUPER STARS ST-351",
                "available_units": 0,
                "average_daily_demand": 2,
            }
        ],
        as_of=date(2026, 7, 31),
    )

    item = report["purchase_proposal"]["items"][0]
    units_per_carton = int(item["units_per_carton"])
    assert units_per_carton == 54
    assert item["recommended_units"] % units_per_carton == 0
    assert item["recommended_cartons"] == pytest.approx(
        item["recommended_units"] / units_per_carton
    )
    assert report["purchase_proposal"]["total_cartons"] == pytest.approx(
        item["recommended_cartons"]
    )


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
