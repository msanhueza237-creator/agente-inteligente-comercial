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


def payload_rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Return business rows from common REST, JSON-LD and nested envelopes."""

    return _payload_rows(payload, keys, depth=0)


def _payload_rows(
    payload: Any, keys: tuple[str, ...], *, depth: int
) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict) or depth > 4:
        return []
    candidate_keys = (
        *keys,
        "data",
        "results",
        "records",
        "items",
        "products",
        "documents",
        "content",
        "rows",
        "hydra:member",
    )
    for key in dict.fromkeys(candidate_keys):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    # Some APIs wrap the collection two or more times under provider-specific
    # names. Search nested dictionaries without depending on their labels.
    for value in payload.values():
        if isinstance(value, list):
            rows = [row for row in value if isinstance(row, dict)]
            if rows:
                return rows
        if isinstance(value, dict):
            rows = _payload_rows(value, keys, depth=depth + 1)
            if rows:
                return rows
    # A few endpoints return an object keyed by record ID instead of an array.
    mapped_rows = [value for value in payload.values() if isinstance(value, dict)]
    if mapped_rows and len(mapped_rows) == len(payload):
        row_markers = {
            "id",
            "sku",
            "code",
            "codigo",
            "codigoProducto",
            "name",
            "nombre",
            "date",
            "fecha",
        }
        if any(row_markers.intersection(row) for row in mapped_rows):
            return mapped_rows
    return []


def _line_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    return payload_rows(
        document,
        "items",
        "lines",
        "details",
        "detail",
        "detalles",
        "detalle",
        "lineas",
        "products",
        "document_items",
        "documentLines",
    )


def _inventory_stock(product: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """Read stock from Facto's documented warehouse inventory structure."""

    inventories = product.get("inventories")
    if not isinstance(inventories, dict):
        return None, []

    details_value = inventories.get("details")
    details = (
        [row for row in details_value if isinstance(row, dict)]
        if isinstance(details_value, list)
        else []
    )
    # Facto's production API can return ``total_available = 0`` even when the
    # per-location details contain the real positive stock.  The Bodega screen
    # is built from those details, so they are the authoritative source when
    # present.  Use the summary only for installations that omit details.
    total_available = None
    if details:
        quantities = [
            _first(row, "available_quantity", "cantidad_disponible", "available")
            for row in details
        ]
        known_quantities = [value for value in quantities if value is not None]
        if known_quantities:
            total_available = sum(
                (_decimal(value) for value in known_quantities),
                Decimal("0"),
            )
    if total_available is None:
        total_available = _first(
            inventories,
            "total_available",
            "available_quantity",
            "total_disponible",
        )
    return total_available, details


def _document_date(document: dict[str, Any]) -> date | None:
    header = document.get("header")
    if isinstance(header, dict):
        nested_date = _document_date(header)
        if nested_date is not None:
            return nested_date
    value = _first(
        document,
        "issued_at",
        "issue_date",
        "date",
        "created_at",
        "emission_date",
        "fecha_emision",
        "fechaEmision",
        "fecha",
    )
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
    for document in payload_rows(documents_payload, "data", "documents", "items"):
        document_date = _document_date(document)
        if document_date:
            document_dates.append(document_date)
        for line in _line_rows(document):
            sku = _first(
                line,
                "sku",
                "product_sku",
                "code",
                "product_code",
                "codigo",
                "codigo_producto",
                "codigoProducto",
            )
            if sku is None:
                product = line.get("product")
                if isinstance(product, dict):
                    sku = _first(product, "sku", "code", "id")
            if sku is None:
                continue
            quantity = _decimal(
                _first(line, "quantity", "qty", "units", "amount", "cantidad")
            )
            if quantity > 0:
                sold_units[str(sku).strip()] += quantity

    if document_dates:
        period_days = max(1, (max(document_dates) - min(document_dates)).days + 1)
    else:
        period_days = 0

    snapshots: list[dict[str, Any]] = []
    for product in payload_rows(products_payload, "data", "products", "items"):
        sku_value = _first(
            product,
            "sku",
            "code",
            "product_code",
            "codigo",
            "codigo_producto",
            "codigoProducto",
            "id",
        )
        if sku_value is None:
            continue
        sku = str(sku_value).strip()
        # Facto keeps the real availability in ``inventories``. Generic
        # ``quantity`` fields belong to invoicing and must not be interpreted
        # as warehouse stock.
        stock_value, warehouse_details = _inventory_stock(product)
        if stock_value is None:
            stock_value = _first(
                product,
                "stock",
                "stock_quantity",
                "available_stock",
                "available_quantity",
                "stock_actual",
                "stockActual",
                "existencia",
            )
        stock_known = stock_value is not None
        available_units = _decimal(stock_value)
        cost_value = _first(
            product,
            "cost_usd",
            "unit_cost_usd",
            "cost",
            "purchase_price",
            "costo",
            "costo_neto",
            "costoNeto",
            "precio_compra",
            "precioCompra",
        )
        cost_currency_id: Any = None
        cost_currency_code: Any = None
        explicit_usd_cost = _first(product, "cost_usd", "unit_cost_usd") is not None
        if isinstance(cost_value, dict):
            cost_currency_id = _first(cost_value, "currency_id", "currencyId")
            cost_currency_code = _first(
                cost_value,
                "currency",
                "currency_code",
                "currencyCode",
            )
            cost_value = _first(cost_value, "value", "amount", "unit_cost", "valor")
        unit_cost = _decimal(cost_value)
        cost_is_usd = explicit_usd_cost or str(cost_currency_code or "").upper() == "USD"
        price_value = _first(
            product,
            "price",
            "sale_price",
            "retail_price",
            "selling_price",
            "precio",
            "precio_neto",
            "precioNeto",
        )
        prices = product.get("prices") or product.get("price")
        price_currency_id: Any = None
        price_currency_code: Any = None
        if isinstance(prices, list) and prices:
            first_price = prices[0]
            if isinstance(first_price, dict):
                price_currency_id = _first(first_price, "currency_id", "currencyId")
                price_currency_code = _first(
                    first_price,
                    "currency",
                    "currency_code",
                    "currencyCode",
                )
                price_value = _first(
                    first_price,
                    "unit_net",
                    "unit_total",
                    "value",
                    "amount",
                )
        elif isinstance(price_value, dict):
            price_currency_id = _first(price_value, "currency_id", "currencyId")
            price_currency_code = _first(
                price_value,
                "currency",
                "currency_code",
                "currencyCode",
            )
            price_value = _first(price_value, "unit_net", "unit_total", "value", "amount")
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
                    "name": str(
                        _first(
                            product,
                            "name",
                            "title",
                            "description",
                            "nombre",
                            "descripcion",
                        )
                        or sku
                    ),
                    "available_units": float(available_units),
                    "stock_known": stock_known,
                    "warehouse_stock": [
                        {
                            "product_location_id": _first(
                                row,
                                "product_location_id",
                                "location_id",
                                "bodega_id",
                            ),
                            "available_quantity": float(
                                _decimal(
                                    _first(
                                        row,
                                        "available_quantity",
                                        "cantidad_disponible",
                                        "available",
                                    )
                                )
                            ),
                            "reserved_quantity": float(
                                _decimal(
                                    _first(
                                        row,
                                        "reserved_quantity",
                                        "cantidad_reservada",
                                        "reserved",
                                    )
                                )
                            ),
                        }
                        for row in warehouse_details
                    ],
                    "unit_cost_usd": float(unit_cost) if cost_is_usd else 0.0,
                    "unit_cost_source": float(unit_cost),
                    "cost_currency_id": cost_currency_id,
                    "cost_currency_code": cost_currency_code,
                    "cost_known": cost_value is not None and cost_is_usd,
                    "cost_available_in_source": cost_value is not None,
                    "cost_requires_usd_conversion": cost_value is not None and not cost_is_usd,
                    "unit_price": float(unit_price),
                    "price_known": price_value is not None,
                    "price_currency_id": price_currency_id,
                    "price_currency_code": price_currency_code,
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
