from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.foreign_trade.planning import ForeignTradePlanner, InventoryPosition


DATA_DIR = Path(__file__).with_name("data")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _plain(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.lower().replace("brand super stars", " ").replace("super stars", " ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _compact_key(value: Any) -> str:
    return _plain(value).replace(" ", "")


def _round(value: Decimal, digits: str = "0.01") -> float:
    return float(value.quantize(Decimal(digits)))


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def load_import_catalog() -> dict[str, Any]:
    return json.loads((DATA_DIR / "import_catalog.json").read_text(encoding="utf-8"))


def load_import_cost_model() -> dict[str, Any]:
    return json.loads((DATA_DIR / "import_cost_model.json").read_text(encoding="utf-8"))


def _match_catalog_item(
    product: dict[str, Any], catalog: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, float, str]:
    sku = _compact_key(product.get("sku"))
    name = _plain(product.get("name"))
    if sku:
        exact = [item for item in catalog if _compact_key(item.get("sku")) == sku]
        if exact:
            return max(exact, key=lambda item: bool(item.get("unit_cbm"))), 1.0, "sku_exact"
    if name:
        exact_name = [item for item in catalog if _plain(item.get("name")) == name]
        if exact_name:
            return max(exact_name, key=lambda item: bool(item.get("unit_cbm"))), 0.98, "name_exact"

    product_tokens = set(name.split())
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in catalog:
        candidate = _plain(item.get("name"))
        if not candidate:
            continue
        candidate_tokens = set(candidate.split())
        overlap = (
            len(product_tokens & candidate_tokens) / max(1, len(product_tokens | candidate_tokens))
        )
        sequence = SequenceMatcher(None, name, candidate).ratio()
        score = sequence * 0.65 + overlap * 0.35
        if score > best_score:
            best, best_score = item, score
    if best is not None and best_score >= 0.76:
        return best, best_score, "name_similarity"
    return None, best_score, "unmatched"


def _ceil_multiple(quantity: int, multiple: Decimal) -> int:
    if multiple <= 1:
        return max(0, quantity)
    return int((Decimal(quantity) / multiple).to_integral_value(rounding=ROUND_UP) * multiple)


def _floor_multiple(quantity: int, multiple: Decimal) -> int:
    if multiple <= 1:
        return max(0, quantity)
    return int((Decimal(quantity) / multiple).to_integral_value(rounding=ROUND_DOWN) * multiple)


def _landed_cost(
    *, unit_fob: Decimal, unit_cbm: Decimal, quantity: int, rates: dict[str, Any]
) -> dict[str, float]:
    fob = unit_fob * quantity
    total_cbm = unit_cbm * quantity
    if total_cbm > 0:
        freight = total_cbm * _decimal(rates.get("freight_usd_per_cbm"))
        local = total_cbm * _decimal(rates.get("local_and_agency_usd_per_cbm"))
    else:
        fallback = _decimal(rates.get("fallback_nonrecoverable_cost_percent_fob")) / 100
        freight = fob * fallback * Decimal("0.55")
        local = fob * fallback * Decimal("0.35")
    insurance = fob * _decimal(rates.get("insurance_percent_fob")) / 100
    cif = fob + freight + insurance
    duty = cif * _decimal(rates.get("customs_duty_percent_cif")) / 100
    if total_cbm <= 0:
        fallback = _decimal(rates.get("fallback_nonrecoverable_cost_percent_fob")) / 100
        known = freight + local + insurance + duty
        local += max(Decimal("0"), fob * fallback - known)
    landed = cif + duty + local
    vat_cash = (cif + duty) * _decimal(rates.get("import_vat_percent")) / 100
    return {
        "fob_usd": _round(fob),
        "freight_usd": _round(freight),
        "insurance_usd": _round(insurance),
        "customs_duty_usd": _round(duty),
        "local_and_agency_usd": _round(local),
        "landed_cost_usd": _round(landed),
        "recoverable_import_vat_cash_usd": _round(vat_cash),
        "total_cbm": _round(total_cbm, "0.0001"),
    }


def _sum_costs(rows: list[dict[str, Any]]) -> dict[str, float]:
    keys = (
        "fob_usd",
        "freight_usd",
        "insurance_usd",
        "customs_duty_usd",
        "local_and_agency_usd",
        "landed_cost_usd",
        "recoverable_import_vat_cash_usd",
        "total_cbm",
    )
    return {key: round(sum(float(row["costs"].get(key, 0)) for row in rows), 2) for key in keys}


def build_foreign_trade_report(
    products: list[dict[str, Any]], *, as_of: date | None = None
) -> dict[str, Any]:
    as_of = as_of or date.today()
    planner = ForeignTradePlanner()
    catalog_payload = load_import_catalog()
    cost_model = load_import_cost_model()
    catalog = [item for item in catalog_payload.get("items", []) if isinstance(item, dict)]
    rates = cost_model["derived_rates"]
    projected_arrival = planner.projected_arrival(as_of)
    demand_multiplier = Decimal("1.25") if projected_arrival.month in planner.high_season_months else Decimal("1")
    evaluated: list[dict[str, Any]] = []

    for product in products:
        item, match_score, match_method = _match_catalog_item(product, catalog)
        if item is None:
            continue
        available = int(_decimal(product.get("available_units")))
        demand = _decimal(product.get("average_daily_demand"))
        unit_fob = _decimal(item.get("unit_fob_usd"))
        unit_cbm = _decimal(item.get("unit_cbm"))
        if unit_fob <= 0:
            continue
        recommendation = planner.recommend(
            InventoryPosition(
                sku=str(product.get("sku") or item.get("sku") or item.get("name")),
                available_units=available,
                committed_units=int(_decimal(product.get("committed_units"))),
                confirmed_inbound_units=int(_decimal(product.get("confirmed_inbound_units"))),
                average_daily_demand=demand,
                unit_cost_usd=unit_fob,
            ),
            as_of=as_of,
            demand_multiplier=demand_multiplier,
        )
        multiple = max(Decimal("1"), _decimal(item.get("order_multiple")))
        units = _ceil_multiple(recommendation.recommended_units, multiple)
        costs = _landed_cost(
            unit_fob=unit_fob, unit_cbm=unit_cbm, quantity=units, rates=rates
        )
        coverage_days = float(Decimal(available) / demand) if demand > 0 else None
        evaluated.append(
            {
                "sku": recommendation.sku,
                "name": product.get("name") or item.get("name"),
                "supplier": item.get("supplier"),
                "available_units": available,
                "average_daily_demand": float(demand),
                "coverage_days": round(coverage_days, 1) if coverage_days is not None else None,
                "recommended_units": units,
                "order_multiple": float(multiple),
                "unit_fob_usd": float(unit_fob),
                "unit_cbm": float(unit_cbm),
                "costs": costs,
                "severity": recommendation.severity,
                "projected_stockout_date": _date_text(recommendation.projected_stockout_date),
                "required_order_date": _date_text(recommendation.required_order_date),
                "projected_arrival_date": projected_arrival.isoformat(),
                "match_score": round(match_score, 3),
                "match_method": match_method,
                "source_document": item.get("source_document"),
                "source_row": item.get("source_row"),
                "volume_evidence": item.get("volume_evidence"),
                "warnings": list(recommendation.warnings),
            }
        )

    replenishment_risks = [row for row in evaluated if row["recommended_units"] > 0]
    missing_volume_risks = [row for row in replenishment_risks if not row["unit_cbm"]]
    candidates = [row for row in replenishment_risks if row["unit_cbm"] > 0]
    candidates.sort(
        key=lambda row: (
            SEVERITY_ORDER.get(str(row["severity"]), 9),
            row["coverage_days"] if row["coverage_days"] is not None else math.inf,
            -row["average_daily_demand"],
        )
    )
    selected: list[dict[str, Any]] = []
    remaining_budget = planner.hard_po_max_usd
    remaining_cbm = _decimal(cost_model["reference"].get("total_cbm"))
    for row in candidates:
        unit_fob = _decimal(row["unit_fob_usd"])
        unit_cbm = _decimal(row["unit_cbm"])
        multiple = max(Decimal("1"), _decimal(row["order_multiple"]))
        max_budget_units = int(remaining_budget / unit_fob) if unit_fob else 0
        max_cbm_units = int(remaining_cbm / unit_cbm) if unit_cbm else row["recommended_units"]
        units = min(int(row["recommended_units"]), max_budget_units, max_cbm_units)
        units = _floor_multiple(units, multiple)
        if units <= 0:
            continue
        selected_row = dict(row)
        selected_row["recommended_units"] = units
        selected_row["costs"] = _landed_cost(
            unit_fob=unit_fob, unit_cbm=unit_cbm, quantity=units, rates=rates
        )
        selected.append(selected_row)
        remaining_budget -= unit_fob * units
        remaining_cbm -= unit_cbm * units
        if remaining_budget <= 0 or remaining_cbm <= 0:
            break

    totals = _sum_costs(selected)
    target_status = (
        "target_range_50000_70000"
        if planner.target_po_min_usd <= _decimal(totals["fob_usd"]) <= planner.hard_po_max_usd
        else "below_target_requires_reason"
        if totals["fob_usd"] > 0
        else "no_purchase"
    )
    warnings: list[str] = []
    if totals["fob_usd"] and totals["fob_usd"] < float(planner.target_po_min_usd):
        warnings.append("La orden consolidada queda bajo USD 50.000 y requiere justificacion comercial.")
    if projected_arrival.month in planner.high_season_months:
        warnings.append("La llegada proyectada coincide con la temporada alta noviembre-febrero.")
    if any(row["match_method"] == "name_similarity" for row in selected):
        warnings.append("Las coincidencias por nombre deben confirmarse antes de aprobar la orden.")
    if missing_volume_risks:
        warnings.append(
            f"{len(missing_volume_risks)} productos con necesidad de reposicion quedan fuera "
            "de la orden hasta confirmar su m3 unitario."
        )
    warnings.append("El IVA de importacion se informa como necesidad de caja recuperable y no como costo del inventario.")

    return {
        "generated_at": as_of.isoformat(),
        "policy": {
            "production_days": planner.policy.production_days,
            "sea_travel_days": planner.policy.sea_travel_days,
            "customs_delay_days": planner.policy.customs_delay_days,
            "lead_time_days": planner.policy.production_days + planner.policy.sea_travel_days + planner.policy.customs_delay_days,
            "safety_stock_days": planner.policy.safety_stock_days,
            "review_period_days": planner.policy.review_period_days,
            "target_coverage_days": planner.policy.target_coverage_days,
            "high_season_months": sorted(planner.high_season_months),
            "factory_shutdown_months": list(planner.policy.factory_shutdown_months),
            "purchase_range_usd": [float(planner.target_po_min_usd), float(planner.hard_po_max_usd)],
            "human_approval_required": True,
        },
        "catalog": {
            **catalog_payload.get("coverage", {}),
            "matched_inventory_products": len(evaluated),
            "matched_with_cbm": sum(bool(row["unit_cbm"]) for row in evaluated),
            "source_documents": catalog_payload.get("sources", []),
            "items": [
                {
                    "sku": item.get("sku"),
                    "name": item.get("name"),
                    "supplier": item.get("supplier"),
                    "unit_fob_usd": item.get("unit_fob_usd"),
                    "unit_cbm": item.get("unit_cbm"),
                    "order_multiple": item.get("order_multiple"),
                    "cartons": item.get("cartons"),
                    "gross_weight_kg": item.get("gross_weight_kg"),
                    "source_document": item.get("source_document"),
                    "source_row": item.get("source_row"),
                    "volume_evidence": item.get("volume_evidence"),
                }
                for item in catalog
            ],
        },
        "historical_cost_reference": cost_model,
        "demand_multiplier": float(demand_multiplier),
        "projected_arrival_date": projected_arrival.isoformat(),
        "products": evaluated,
        "pending_volume_products": missing_volume_risks,
        "purchase_proposal": {
            "status": target_status,
            "items": selected,
            "totals": totals,
            "container_reference_cbm": float(_decimal(cost_model["reference"].get("total_cbm"))),
            "container_utilization_percent": round(
                totals["total_cbm"] / float(_decimal(cost_model["reference"].get("total_cbm"))) * 100,
                1,
            )
            if totals["total_cbm"]
            else 0,
            "required_order_date": min(
                (row["required_order_date"] for row in selected if row["required_order_date"]),
                default=None,
            ),
            "projected_arrival_date": projected_arrival.isoformat(),
            "warnings": warnings,
        },
        "methodology": (
            "Cruce exacto por SKU y, en segundo termino, similitud de nombre. La demanda proviene de ventas Facto; "
            "el volumen y FOB provienen de documentos Chinafore; los costos logisticos usan el despacho real 49194."
        ),
    }
