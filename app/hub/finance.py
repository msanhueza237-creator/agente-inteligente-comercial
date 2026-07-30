from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.hub.inventory import payload_rows

VAT_FACTOR = Decimal("1.19")
MONTH_LABELS = (
    "Ene",
    "Feb",
    "Mar",
    "Abr",
    "May",
    "Jun",
    "Jul",
    "Ago",
    "Sep",
    "Oct",
    "Nov",
    "Dic",
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(".", "").replace(",", ".") if "," in value else value.strip()
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _nested_dict(document: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = document.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _document_date(document: dict[str, Any]) -> date | None:
    header = _nested_dict(document, "header", "encabezado")
    value = _first(
        header,
        "issue_date",
        "emission_date",
        "document_date",
        "fecha_emision",
        "fecha",
    ) or _first(
        document,
        "issue_date",
        "emission_date",
        "document_date",
        "fecha_emision",
        "fecha",
    )
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _document_type_id(document: dict[str, Any]) -> int | None:
    header = _nested_dict(document, "header", "encabezado")
    value = _first(document, "document_type_id", "type_id") or _first(
        header, "document_type_id", "type_id"
    )
    if isinstance(value, dict):
        value = _first(value, "document_type_id", "id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _amounts(document: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    header = _nested_dict(document, "header", "encabezado")
    totals = _nested_dict(document, "totals", "total", "amounts", "montos")
    candidates = [totals, header, document]

    def find(*keys: str) -> Decimal | None:
        for candidate in candidates:
            value = _decimal(_first(candidate, *keys))
            if value is not None:
                return value
        return None

    net = find(
        "net",
        "net_amount",
        "amount_net",
        "net_total",
        "total_net",
        "subtotal",
        "monto_neto",
    )
    tax = find("tax", "tax_amount", "amount_tax", "vat", "iva", "monto_iva")
    gross = find(
        "gross",
        "gross_amount",
        "total_amount",
        "amount_total",
        "grand_total",
        "monto_total",
    )

    # Exempt invoices have no VAT; their total is already net.
    exempt = _document_type_id(document) == 28
    if net is None and gross is not None:
        net = gross if exempt else (gross / VAT_FACTOR)
    if gross is None and net is not None:
        gross = net if exempt else net + (tax if tax is not None else net * Decimal("0.19"))
    if tax is None and gross is not None and net is not None:
        tax = max(Decimal("0"), gross - net)

    # Some Facto detail variants expose totals only in their lines.
    if net is None:
        net = Decimal("0")
        for line in payload_rows(document, "details", "items", "lines", "document_items"):
            quantity = _decimal(_first(line, "quantity", "qty", "cantidad")) or Decimal("0")
            unit_net = _decimal(
                _first(line, "unit_net", "net_unit_price", "precio_neto", "unit_price")
            )
            line_net = _decimal(_first(line, "net_total", "line_net", "monto_neto"))
            net += line_net if line_net is not None else quantity * (unit_net or Decimal("0"))
        gross = net if exempt else net * VAT_FACTOR
        tax = gross - net

    return net or Decimal("0"), tax or Decimal("0"), gross or Decimal("0")


def _customer(document: dict[str, Any]) -> tuple[str, str]:
    header = _nested_dict(document, "header", "encabezado")
    party = (
        _nested_dict(document, "customer", "client", "receiver", "recipient", "receptor")
        or _nested_dict(header, "customer", "client", "receiver", "recipient", "receptor")
    )
    candidates = (party, header, document)

    def find(*keys: str) -> Any:
        for candidate in candidates:
            value = _first(candidate, *keys)
            if value not in (None, ""):
                return value
        return None

    # Facto expone al receptor tanto como objeto anidado como mediante campos
    # planos receiver_* en el encabezado, según la versión del documento.
    name = str(
        find(
            "receiver_legal_name",
            "receiver_business_name",
            "receiver_name",
            "receiverLegalName",
            "receiverBusinessName",
            "receiverName",
            "recipient_legal_name",
            "recipient_business_name",
            "recipient_name",
            "customer_legal_name",
            "customer_business_name",
            "customer_name",
            "client_legal_name",
            "client_business_name",
            "client_name",
            "receptor_razon_social",
            "receptor_nombre",
            "razon_social_receptor",
            "business_name",
            "legal_name",
            "name",
            "razon_social",
            "trade_name",
        )
        or "Cliente no identificado"
    ).strip()
    tax_id = str(
        find(
            "receiver_tax_id_code",
            "receiver_tax_id",
            "receiver_rut",
            "receiverTaxIdCode",
            "receiverTaxId",
            "recipient_tax_id_code",
            "recipient_tax_id",
            "recipient_rut",
            "customer_tax_id_code",
            "customer_tax_id",
            "customer_rut",
            "client_tax_id_code",
            "client_tax_id",
            "client_rut",
            "receptor_rut",
            "rut_receptor",
            "tax_id_code",
            "tax_id",
            "rut",
            "document_number",
            "identifier",
        )
        or ""
    ).strip()
    return name, tax_id


def _annual_comparison(
    dated_amounts: list[tuple[date, Decimal]],
    *,
    generated_on: date,
) -> dict[str, Any]:
    """Compare current YTD sales with the equivalent period a year earlier."""

    current_year = generated_on.year
    previous_year = current_year - 1
    # The cutoff is either supplied by the caller or derived from the latest
    # issued document, so the comparison never extends past synchronized data.
    cutoff = generated_on
    try:
        previous_cutoff = cutoff.replace(year=previous_year)
    except ValueError:
        # 29 February compares against 28 February in a non-leap prior year.
        previous_cutoff = cutoff.replace(year=previous_year, day=28)

    monthly_current = {month: Decimal("0") for month in range(1, 13)}
    monthly_previous = {month: Decimal("0") for month in range(1, 13)}
    current_ytd = Decimal("0")
    previous_ytd = Decimal("0")
    previous_full_year = Decimal("0")
    current_documents = 0
    previous_documents = 0

    for issued, net in dated_amounts:
        if issued.year == current_year:
            monthly_current[issued.month] += net
            if issued <= cutoff:
                current_ytd += net
                current_documents += 1
        elif issued.year == previous_year:
            monthly_previous[issued.month] += net
            previous_full_year += net
            if issued <= previous_cutoff:
                previous_ytd += net
                previous_documents += 1

    growth_amount = current_ytd - previous_ytd
    growth_percent = (
        (growth_amount / previous_ytd) * Decimal("100")
        if previous_ytd
        else None
    )
    return {
        "current_year": current_year,
        "previous_year": previous_year,
        "cutoff_date": cutoff.isoformat(),
        "previous_cutoff_date": previous_cutoff.isoformat(),
        "current_ytd_net_sales": float(current_ytd),
        "previous_ytd_net_sales": float(previous_ytd),
        "previous_full_year_net_sales": float(previous_full_year),
        "growth_amount": float(growth_amount),
        "growth_percent": float(growth_percent) if growth_percent is not None else None,
        "current_ytd_documents": current_documents,
        "previous_ytd_documents": previous_documents,
        "months": [
            {
                "month": month,
                "label": MONTH_LABELS[month - 1],
                "current_net_sales": float(monthly_current[month]),
                "previous_net_sales": float(monthly_previous[month]),
            }
            for month in range(1, 13)
        ],
    }


def extract_financial_snapshot(
    documents_payload: Any,
    product_snapshots: list[dict[str, Any]] | None = None,
    *,
    generated_on: date | None = None,
) -> list[dict[str, Any]]:
    """Create one auditable rolling financial summary from issued Facto documents.

    The summary intentionally excludes bank balance, paid/unpaid status and
    expenses until Facto supplies those resources explicitly.
    """

    documents = payload_rows(documents_payload, "data", "documents", "items")
    monthly: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"net_sales": Decimal("0"), "tax": Decimal("0"), "gross_sales": Decimal("0"), "documents": 0}
    )
    customers: dict[str, dict[str, Any]] = {}
    document_types: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"net_sales": Decimal("0"), "documents": 0}
    )
    dates: list[date] = []
    dated_amounts: list[tuple[date, Decimal]] = []
    net_sales = Decimal("0")
    tax = Decimal("0")
    gross_sales = Decimal("0")

    for document in documents:
        if not isinstance(document, dict):
            continue
        issued = _document_date(document)
        if issued:
            dates.append(issued)
        net, document_tax, gross = _amounts(document)
        if issued:
            dated_amounts.append((issued, net))
        net_sales += net
        tax += document_tax
        gross_sales += gross
        month = issued.strftime("%Y-%m") if issued else "sin_fecha"
        monthly[month]["net_sales"] += net
        monthly[month]["tax"] += document_tax
        monthly[month]["gross_sales"] += gross
        monthly[month]["documents"] += 1
        document_type = str(_document_type_id(document) or "otro")
        document_types[document_type]["net_sales"] += net
        document_types[document_type]["documents"] += 1
        customer_name, customer_tax_id = _customer(document)
        customer_key = customer_tax_id or customer_name.casefold()
        customer = customers.setdefault(
            customer_key,
            {"name": customer_name, "tax_id": customer_tax_id, "net_sales": Decimal("0"), "documents": 0},
        )
        customer["net_sales"] += net
        customer["documents"] += 1

    products: list[dict[str, Any]] = []
    reference_cost = Decimal("0")
    for item in product_snapshots or []:
        payload = item.get("payload", item) if isinstance(item, dict) else {}
        units = _decimal(payload.get("units_sold_observed")) or Decimal("0")
        revenue = _decimal(payload.get("sales_revenue_observed")) or Decimal("0")
        unit_cost = _decimal(payload.get("unit_cost_source")) or Decimal("0")
        if units <= 0:
            continue
        cost = units * unit_cost if payload.get("cost_available_in_source") else Decimal("0")
        reference_cost += cost
        products.append(
            {
                "sku": payload.get("sku"),
                "name": payload.get("name"),
                "units": float(units),
                "net_sales_observed": float(revenue),
                "reference_cost": float(cost),
            }
        )

    products.sort(key=lambda item: item["net_sales_observed"], reverse=True)
    top_customers = sorted(customers.values(), key=lambda item: item["net_sales"], reverse=True)
    document_count = len(documents)
    comparison_cutoff = generated_on or (max(dates) if dates else date.today())
    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "period_start": min(dates).isoformat() if dates else None,
        "period_end": max(dates).isoformat() if dates else None,
        "document_count": document_count,
        "net_sales": float(net_sales),
        "tax": float(tax),
        "gross_sales": float(gross_sales),
        "average_net_ticket": float(net_sales / document_count) if document_count else 0,
        "reference_cost_of_sales": float(reference_cost),
        "reference_gross_margin": float(net_sales - reference_cost),
        "reference_margin_available": bool(products and reference_cost),
        "sales_by_month": [
            {"month": month, **{key: float(value) if isinstance(value, Decimal) else value for key, value in values.items()}}
            for month, values in sorted(monthly.items())
        ],
        "year_comparison": _annual_comparison(
            dated_amounts,
            generated_on=comparison_cutoff,
        ),
        "document_types": [
            {"document_type_id": key, **{name: float(value) if isinstance(value, Decimal) else value for name, value in values.items()}}
            for key, values in document_types.items()
        ],
        # El CRM necesita la cartera completa para buscar por RUT o razón social.
        # El nombre se conserva por compatibilidad con snapshots ya publicados.
        "top_customers": [
            {**item, "net_sales": float(item["net_sales"])} for item in top_customers
        ],
        "customer_count": len(top_customers),
        "top_products": products[:30],
        "receivables_available": False,
        "expenses_available": False,
        "cash_balance_available": False,
        "source": "facto_read_only",
    }
    return [{"external_id": "rolling-sales-365", "payload": snapshot}]
