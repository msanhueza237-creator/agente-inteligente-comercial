"""Conservative inventory normalization for read-only ERP data.

Facto installations can expose slightly different field names.  This module
only derives a metric when the source field is explicit; it never guesses
stock, cost or demand.  The resulting snapshots are safe to send to the CRM
because they contain product data only, not credentials or customer details.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value).replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return default


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _line_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    return _rows(
        document,
        "items",
        "lines",
        "details",
        "products",
        "document_items",
        "documentLines",
    )


def _document_date(document: dict[str, Any]) -> date | None:
    value = _first(document, "issued_at", "issue_date", "date", "created_at", "emission_date")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def extract_product_snapshots(products_payload: Any, documents_payload: Any) -> list[dict[str, Any]]:
    """Build SKU snapshots using explicit product stock/cost and sale documents.

    Demand is calculated from the observation range present in Facto's first
    document page.  A ``demand_available`` flag protects the purchase agent
    from proposing an order when sales history is not yet complete.
    """

    sold_units: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    document_dates: list[date] = []
    for document in _rows(documents_payload, "data", "documents", "items"):
        document_date = _document_date(document)
        if document_date:
            document_dates.append(document_date)
        for line in _line_rows(document):
            sku = _first(line, "sku", "product_sku", "code", "product_code")
            if sku is None:
                product = line.get("product")
                if isinstance(product, dict):
                    sku = _first(product, "sku", "code", "id")
            if sku is None:
                continue
            quantity = _decimal(_first(line, "quantity", "qty", "units", "amount"))
            if quantity > 0:
                sold_units[str(sku).strip()] += quantity

    if document_dates:
        period_days = max(1, (max(document_dates) - min(document_dates)).days + 1)
    else:
        period_days = 0

    snapshots: list[dict[str, Any]] = []
    for product in _rows(products_payload, "data", "products", "items"):
        sku_value = _first(product, "sku", "code", "product_code", "id")
        if sku_value is None:
            continue
        sku = str(sku_value).strip()
        stock_value = _first(
            product,
            "stock",
            "stock_quantity",
            "available_stock",
            "available_quantity",
            "quantity",
            "inventory",
        )
        stock_known = stock_value is not None
        available_units = _decimal(stock_value)
        cost_value = _first(product, "cost_usd", "unit_cost_usd", "cost", "purchase_price")
        unit_cost = _decimal(cost_value)
        price_value = _first(product, "price", "sale_price", "retail_price", "selling_price")
        unit_price = _decimal(price_value)
        units_sold = sold_units[sku]
        demand = (units_sold / Decimal(period_days)) if period_days else Decimal("0")
        margin_value = unit_price - unit_cost if cost_value is not None and price_value is not None else None
        margin_pct = (
            (margin_value / unit_price * Decimal("100"))
            if margin_value is not None and unit_price > 0
            else None
        )
        snapshots.append(
            {
                "external_id": sku,
                "payload": {
                    "sku": sku,
                    "name": str(_first(product, "name", "title", "description") or sku),
                    "available_units": float(available_units),
                    "stock_known": stock_known,
                    "unit_cost_usd": float(unit_cost),
                    "cost_known": cost_value is not None,
                    "unit_price": float(unit_price),
                    "price_known": price_value is not None,
                    "unit_margin": float(margin_value) if margin_value is not None else None,
                    "margin_percent": float(margin_pct) if margin_pct is not None else None,
                    "average_daily_demand": float(demand),
                    "demand_available": bool(period_days and units_sold > 0),
                    "sales_history_available": bool(period_days),
                    "units_sold_observed": float(units_sold),
                    "demand_observation_days": period_days,
                    "source": "facto_read_only",
                },
            }
        )
    return snapshots


def eligible_replenishment_payloads(snapshots: Iterable[dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
    """Return only evidence-backed product inputs for the purchase agent."""

    result: list[dict[str, Any]] = []
    for snapshot in snapshots:
        payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
        if not isinstance(payload, dict):
            continue
        if not (payload.get("stock_known") and payload.get("cost_known") and payload.get("demand_available")):
            continue
        result.append(
            {
                "sku": payload["sku"],
                "available_units": payload["available_units"],
                "committed_units": 0,
                "confirmed_inbound_units": 0,
                "average_daily_demand": payload["average_daily_demand"],
                "unit_cost_usd": payload["unit_cost_usd"],
                "as_of": as_of,
                "evidence": {
                    "source": "facto_read_only",
                    "observation_days": payload["demand_observation_days"],
                    "units_sold_observed": payload["units_sold_observed"],
                },
            }
        )
    return result
