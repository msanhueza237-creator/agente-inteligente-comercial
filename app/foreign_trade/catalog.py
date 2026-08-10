from __future__ import annotations

import json
import math
import re
import unicodedata
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.foreign_trade.planning import ForeignTradePlanner, InventoryPosition


DATA_DIR = Path(__file__).with_name("data")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
FREIGHT_PROVIDER_ALIASES = (
    "ad cargas internacional",
    "ads cargas internacional",
    "ads internacional cargo",
    "adscargas",
)
FREIGHT_TEXT_MARKERS = (
    "flete internacional",
    "flete maritimo",
    "ocean freight",
    "freight",
    "20gp",
    "20 pie",
    "20pies",
)
CUSTOMS_REFERENCE_CONTACT = "j.rodriguez@agenciarodriguezpalma.cl"
CUSTOMS_REFERENCE_DOMAIN = "agenciarodriguezpalma.cl"


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


def load_active_imports() -> dict[str, Any]:
    path = DATA_DIR / "active_imports.json"
    if not path.exists():
        return {"schema_version": 1, "imports": [], "sources": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_freight_history() -> dict[str, Any]:
    return json.loads((DATA_DIR / "ads_freight_history.json").read_text(encoding="utf-8"))


def load_customs_cost_references() -> dict[str, Any]:
    return json.loads(
        (DATA_DIR / "customs_cost_references.json").read_text(encoding="utf-8")
    )


def _walk_mappings(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_mappings(nested)


def _first_nested(document: dict[str, Any], *keys: str) -> Any:
    mappings = list(_walk_mappings(document))
    for key in keys:
        for mapping in mappings:
            value = mapping.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                return value
    return None


def _supplier_name(document: dict[str, Any]) -> str:
    return str(
        _first_nested(
            document,
            "issuer_legal_name",
            "issuer_name",
            "supplier_legal_name",
            "supplier_name",
            "provider_name",
            "vendor_name",
            "razon_social_emisor",
            "razon_social",
            "business_name",
            "name",
        )
        or ""
    ).strip()


def _is_ad_cargas_document(document: dict[str, Any]) -> bool:
    supplier = _plain(_supplier_name(document))
    if any(alias in supplier for alias in FREIGHT_PROVIDER_ALIASES):
        return True
    searchable = _plain(json.dumps(document, ensure_ascii=False, default=str))
    return "adscargas cl" in searchable or any(
        alias in searchable for alias in FREIGHT_PROVIDER_ALIASES
    )


def _parse_invoice_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _invoice_date(document: dict[str, Any]) -> date | None:
    return _parse_invoice_date(
        _first_nested(
            document,
            "issue_date",
            "emission_date",
            "invoice_date",
            "document_date",
            "fecha_emision",
            "fecha",
            "receive_date",
            "crm_updated_at",
        )
    )


def _invoice_number(document: dict[str, Any]) -> str:
    value = _first_nested(
        document,
        "folio",
        "invoice_number",
        "document_number",
        "reference_number",
        "number",
        "document_id",
        "crm_external_id",
        "id",
    )
    return str(value or "sin_folio").strip()


def _currency_code(document: dict[str, Any]) -> str:
    value = _first_nested(
        document,
        "currency_code",
        "currency",
        "currency_iso",
        "moneda",
        "currency_name",
    )
    return str(value or "").strip().upper()


def _freight_amount_usd(document: dict[str, Any]) -> tuple[Decimal, str]:
    explicit_usd = _decimal(
        _first_nested(
            document,
            "freight_usd",
            "ocean_freight_usd",
            "international_freight_usd",
            "amount_usd",
            "total_usd",
            "net_usd",
        )
    )
    if explicit_usd > 0:
        return explicit_usd, "explicit_usd"

    currency = _currency_code(document)
    net_amount = _decimal(
        _first_nested(
            document,
            "net_amount",
            "net_total",
            "subtotal",
            "monto_neto",
            "amount",
            "total_amount",
            "total",
        )
    )
    if net_amount <= 0:
        return Decimal("0"), "amount_missing"
    if currency in {"USD", "US$", "DOLAR", "DOLARES", "DOLLAR", "DOLLARS"}:
        return net_amount, "document_currency_usd"

    exchange_rate = _decimal(
        _first_nested(
            document,
            "usd_clp_exchange_rate",
            "exchange_rate_usd_clp",
            "exchange_rate",
            "tipo_cambio",
        )
    )
    if currency in {"CLP", "PESO", "PESOS", "$"} and exchange_rate > 0:
        return net_amount / exchange_rate, "document_clp_with_exchange_rate"
    return Decimal("0"), "currency_or_exchange_rate_missing"


def resolve_freight_history(
    freight_invoices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Prioriza facturas AD/ADS Cargas de Facto y conserva el historico como respaldo."""
    history = deepcopy(load_freight_history())
    historical = [
        {**row, "evidence_source": "historical_verified"}
        for row in history.get("verified_ocean_freight", [])
        if isinstance(row, dict)
    ]
    crm_candidates: list[dict[str, Any]] = []
    for document in freight_invoices or []:
        if not isinstance(document, dict) or not _is_ad_cargas_document(document):
            continue
        amount_usd, conversion_basis = _freight_amount_usd(document)
        issued_at = _invoice_date(document)
        searchable = _plain(json.dumps(document, ensure_ascii=False, default=str))
        crm_candidates.append(
            {
                "invoice_number": _invoice_number(document),
                "invoice_date": issued_at.isoformat() if issued_at else None,
                "route": str(_first_nested(document, "route", "lane", "ruta") or history.get("lane") or ""),
                "container_type": str(
                    _first_nested(document, "container_type", "container", "tipo_contenedor")
                    or (history.get("container_policy") or {}).get("type")
                    or "20GP"
                ),
                "amount_usd": _round(amount_usd) if amount_usd > 0 else None,
                "currency": _currency_code(document) or None,
                "conversion_basis": conversion_basis,
                "freight_description_confirmed": any(marker in searchable for marker in FREIGHT_TEXT_MARKERS),
                "supplier": _supplier_name(document),
                "evidence_source": "crm_facto_purchase_invoice",
                "source": {
                    "system": "crm",
                    "provider": "facto",
                    "resource": document.get("crm_resource") or "purchase_document",
                    "external_id": document.get("crm_external_id"),
                    "synced_at": document.get("crm_updated_at"),
                },
            }
        )

    usable_crm = [
        row for row in crm_candidates if row.get("invoice_date") and row.get("amount_usd")
    ]
    historical.sort(key=lambda row: str(row.get("invoice_date") or ""))
    usable_crm.sort(key=lambda row: str(row.get("invoice_date") or ""))
    verified = [*historical, *usable_crm]
    verified.sort(
        key=lambda row: (
            str(row.get("invoice_date") or ""),
            row.get("evidence_source") == "crm_facto_purchase_invoice",
        )
    )
    # Una factura sincronizada desde Facto es la evidencia operativa vigente del
    # CRM. El historico curado solo se usa cuando el CRM aun no entrega una
    # factura de AD/ADS Cargas valorizable y fechada.
    latest = usable_crm[-1] if usable_crm else (historical[-1] if historical else {})
    amounts = [float(row["amount_usd"]) for row in verified if row.get("amount_usd")]
    latest_source = str(latest.get("evidence_source") or "historical_verified")
    history["verified_ocean_freight"] = verified
    history["crm_facto_candidates"] = crm_candidates
    history["summary"] = {
        "latest_invoice_number": latest.get("invoice_number"),
        "latest_invoice_date": latest.get("invoice_date"),
        "latest_verified_usd": float(latest.get("amount_usd") or 0),
        "latest_provider": latest.get("supplier") or (history.get("provider") or {}).get("name"),
        "latest_source": latest_source,
        "historical_min_usd": min(amounts) if amounts else 0,
        "historical_max_usd": max(amounts) if amounts else 0,
        "historical_average_usd": round(sum(amounts) / len(amounts), 2) if amounts else 0,
        "crm_invoice_candidates": len(crm_candidates),
        "crm_usable_invoices": len(usable_crm),
        "fallback_used": latest_source != "crm_facto_purchase_invoice",
        "selection_basis": (
            "latest_usable_ad_cargas_invoice_from_crm_facto"
            if latest_source == "crm_facto_purchase_invoice"
            else "latest_verified_historical_invoice"
        ),
    }
    history["selection_policy"] = "crm_facto_ad_cargas_first_then_verified_history"
    return history


def _is_agency_rodriguez_reference(document: dict[str, Any]) -> bool:
    searchable = str(json.dumps(document, ensure_ascii=False, default=str)).lower()
    return CUSTOMS_REFERENCE_DOMAIN in searchable or CUSTOMS_REFERENCE_CONTACT in searchable


def resolve_customs_cost_references(
    email_documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Consolida evidencia Gmail de Agencia Rodriguez Palma sin fijar tarifas futuras."""
    payload = deepcopy(load_customs_cost_references())
    documents = [
        row
        for row in payload.get("verified_email_documents", [])
        if isinstance(row, dict)
    ]
    by_key = {
        str(row.get("message_id") or f"{row.get('email_date')}:{row.get('subject')}"): row
        for row in documents
    }
    for row in email_documents or []:
        if not isinstance(row, dict) or not _is_agency_rodriguez_reference(row):
            continue
        message_id = str(
            _first_nested(
                row,
                "message_id",
                "gmail_message_id",
                "external_id",
                "crm_external_id",
                "id",
            )
            or ""
        ).strip()
        email_date = _parse_invoice_date(
            _first_nested(row, "email_date", "date", "received_at", "sent_at", "crm_updated_at")
        )
        raw_attachments = row.get("attachment_names") or row.get("attachments")
        if isinstance(raw_attachments, list):
            attachments = [
                str(item).strip()
                for item in raw_attachments
                if str(item or "").strip()
            ]
        else:
            attachment = _first_nested(row, "attachment", "attachment_name", "filename")
            attachments = [str(attachment).strip()] if attachment else []
        normalized = {
            "message_id": message_id or f"gmail:{len(by_key) + 1}",
            "email_date": email_date.isoformat() if email_date else None,
            "sender": str(_first_nested(row, "sender", "from", "from_") or "").strip(),
            "reference_contact": CUSTOMS_REFERENCE_CONTACT,
            "subject": str(_first_nested(row, "subject") or "").strip(),
            "dispatch": str(_first_nested(row, "dispatch", "despacho") or "").strip() or None,
            "document_type": str(
                _first_nested(row, "document_type", "type") or "supporting_document"
            ).strip(),
            "attachments": attachments,
            "source": "crm_gmail_sync",
            "crm_resource": row.get("crm_resource"),
            "crm_updated_at": row.get("crm_updated_at"),
        }
        by_key[normalized["message_id"]] = normalized

    verified = list(by_key.values())
    verified.sort(key=lambda row: str(row.get("email_date") or ""), reverse=True)
    policy = payload.get("reference_policy") or {}
    payload["verified_email_documents"] = verified
    payload["summary"] = {
        "verified_documents": len(verified),
        "latest_email_date": verified[0].get("email_date") if verified else None,
        "latest_dispatch": verified[0].get("dispatch") if verified else None,
        "latest_subject": verified[0].get("subject") if verified else None,
        "reference_contact": policy.get("contact_email") or CUSTOMS_REFERENCE_CONTACT,
        "accepted_domain": policy.get("accepted_domain") or CUSTOMS_REFERENCE_DOMAIN,
        "usage": "historical_reference_only",
        "fixed_tariff": False,
        "costs_are_variable": True,
    }
    return payload


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


def _consolidated_costs(
    rows: list[dict[str, Any]], *, rates: dict[str, Any], freight_history: dict[str, Any]
) -> dict[str, float]:
    """Calcula la orden y prorratea el flete 20GP por el volumen utilizado."""
    row_totals = _sum_costs(rows)
    fob = _decimal(row_totals.get("fob_usd"))
    total_cbm = _decimal(row_totals.get("total_cbm"))
    if fob <= 0:
        return row_totals

    container_policy = freight_history.get("container_policy") or {}
    capacity = max(Decimal("1"), _decimal(container_policy.get("planning_capacity_cbm")))
    latest_freight = _decimal((freight_history.get("summary") or {}).get("latest_verified_usd"))
    freight = latest_freight * total_cbm / capacity
    insurance = fob * _decimal(rates.get("insurance_percent_fob")) / 100
    cif = fob + freight + insurance
    duty = cif * _decimal(rates.get("customs_duty_percent_cif")) / 100
    if total_cbm > 0:
        local = total_cbm * _decimal(rates.get("local_and_agency_usd_per_cbm"))
    else:
        local = fob * _decimal(rates.get("fallback_nonrecoverable_cost_percent_fob")) / 100
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


def build_foreign_trade_report(
    products: list[dict[str, Any]],
    *,
    as_of: date | None = None,
    freight_invoices: list[dict[str, Any]] | None = None,
    customs_cost_references: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    as_of = as_of or date.today()
    planner = ForeignTradePlanner()
    catalog_payload = load_import_catalog()
    cost_model = load_import_cost_model()
    freight_history = resolve_freight_history(freight_invoices)
    customs_references = resolve_customs_cost_references(customs_cost_references)
    active_import_payload = load_active_imports()
    catalog = [item for item in catalog_payload.get("items", []) if isinstance(item, dict)]
    active_imports = [
        item for item in active_import_payload.get("imports", []) if isinstance(item, dict)
    ]
    active_items = [
        {
            **item,
            "supplier": active_import.get("supplier"),
            "order_number": active_import.get("order_number"),
            "source_document": (active_import.get("source") or {}).get("file"),
            "source_row": item.get("source_line_label") or item.get("source_line_number") or item.get("line_number"),
        }
        for active_import in active_imports
        for item in active_import.get("items", [])
        if isinstance(item, dict)
    ]
    container_policy = freight_history.get("container_policy") or {}
    container_capacity_cbm = max(
        Decimal("1"), _decimal(container_policy.get("planning_capacity_cbm"))
    )
    latest_container_freight = _decimal(
        (freight_history.get("summary") or {}).get("latest_verified_usd")
    )
    rates = dict(cost_model["derived_rates"])
    if latest_container_freight > 0:
        rates["freight_usd_per_cbm"] = float(
            latest_container_freight / container_capacity_cbm
        )
    projected_arrival = planner.projected_arrival(as_of)
    demand_multiplier = Decimal("1.25") if projected_arrival.month in planner.high_season_months else Decimal("1")
    evaluated: list[dict[str, Any]] = []

    for product in products:
        item, match_score, match_method = _match_catalog_item(product, catalog)
        if item is None:
            continue
        active_item, active_match_score, active_match_method = _match_catalog_item(
            product, active_items
        )
        active_inbound_units = (
            int(_decimal(active_item.get("quantity"))) if active_item is not None else 0
        )
        source_inbound_units = int(_decimal(product.get("confirmed_inbound_units")))
        confirmed_inbound_units = source_inbound_units + active_inbound_units
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
                confirmed_inbound_units=confirmed_inbound_units,
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
                "confirmed_inbound_units": confirmed_inbound_units,
                "active_import_inbound_units": active_inbound_units,
                "active_import_orders": [active_item.get("order_number")] if active_item else [],
                "active_import_match_score": round(active_match_score, 3) if active_item else 0,
                "active_import_match_method": active_match_method if active_item else "unmatched",
                "average_daily_demand": float(demand),
                "coverage_days": round(coverage_days, 1) if coverage_days is not None else None,
                "recommended_units": units,
                "order_multiple": float(multiple),
                "units_per_carton": float(multiple),
                "recommended_cartons": _round(Decimal(units) / multiple),
                "packing_box_source": item.get("source_document"),
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
    remaining_cbm = container_capacity_cbm
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
        selected_row["recommended_cartons"] = _round(Decimal(units) / multiple)
        selected_row["costs"] = _landed_cost(
            unit_fob=unit_fob, unit_cbm=unit_cbm, quantity=units, rates=rates
        )
        selected.append(selected_row)
        remaining_budget -= unit_fob * units
        remaining_cbm -= unit_cbm * units
        if remaining_budget <= 0 or remaining_cbm <= 0:
            break

    totals = _consolidated_costs(selected, rates=rates, freight_history=freight_history)
    target_status = (
        "target_range_50000_70000"
        if planner.target_po_min_usd <= _decimal(totals["fob_usd"]) <= planner.hard_po_max_usd
        else "below_target_requires_reason"
        if totals["fob_usd"] > 0
        else "no_purchase"
    )
    warnings: list[str] = []
    freight_summary = freight_history.get("summary") or {}
    if freight_summary.get("fallback_used"):
        if freight_summary.get("crm_invoice_candidates"):
            warnings.append(
                "Se encontraron facturas AD/ADS Cargas en el CRM, pero ninguna trae fecha, moneda USD "
                "o tipo de cambio suficiente para valorizar el flete; se mantiene la ultima referencia "
                "historica verificada."
            )
        else:
            warnings.append(
                "No hay una factura AD/ADS Cargas utilizable en el CRM para este corte; se mantiene "
                "la ultima referencia historica verificada."
            )
    warnings.append(
        "Seguro, derechos, gastos locales y honorarios de agencia son referencias historicas variables. "
        "Antes de aprobar una compra se deben validar contra la factura o solicitud de fondos vigente de "
        "Agencia Rodriguez Palma en Gmail."
    )
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
    utilization_percent = (
        float(_decimal(totals.get("total_cbm")) / container_capacity_cbm * 100)
        if totals["total_cbm"] and container_capacity_cbm
        else 0
    )
    remaining_container_cbm = max(
        Decimal("0"), container_capacity_cbm - _decimal(totals.get("total_cbm"))
    )
    freight_proration_factor = (
        _decimal(totals.get("total_cbm")) / container_capacity_cbm
        if totals.get("total_cbm")
        else Decimal("0")
    )
    if totals["total_cbm"]:
        warnings.append(
            f"El flete de referencia USD {_round(latest_container_freight)} corresponde al "
            f"20GP completo de {_round(container_capacity_cbm)} m3. Esta propuesta usa "
            f"{_round(freight_proration_factor * 100)}% de esa capacidad y reconoce solo "
            f"USD {_round(_decimal(totals['freight_usd']))} de flete proporcional."
        )
    if totals["total_cbm"] and utilization_percent < float(
        _decimal(container_policy.get("target_fill_percent"))
    ):
        warnings.append(
            "El contenedor 20GP queda bajo el objetivo de llenado; faltan "
            f"{_round(remaining_container_cbm, '0.01')} m3 por consolidar."
        )
    warnings.append("El IVA de importacion se informa como necesidad de caja recuperable y no como costo del inventario.")

    active_import_reports: list[dict[str, Any]] = []
    for active_import in active_imports:
        production_start = date.fromisoformat(str(active_import["production_start_date"]))
        production_end = production_start + timedelta(days=planner.policy.production_days)
        port_arrival = production_end + timedelta(days=planner.policy.sea_travel_days)
        warehouse_arrival = port_arrival + timedelta(days=planner.policy.customs_delay_days)
        elapsed_production_days = max(
            0, min(planner.policy.production_days, (as_of - production_start).days)
        )
        active_totals = active_import.get("totals") or {}
        active_cost_row = {
            "costs": _landed_cost(
                unit_fob=_decimal(active_totals.get("fob_usd")),
                unit_cbm=_decimal(active_totals.get("total_cbm")),
                quantity=1,
                rates=rates,
            )
        }
        estimated_costs = _consolidated_costs(
            [active_cost_row], rates=rates, freight_history=freight_history
        )
        active_import_reports.append(
            {
                **active_import,
                "timeline": {
                    "production_days": planner.policy.production_days,
                    "sea_travel_days": planner.policy.sea_travel_days,
                    "customs_days": planner.policy.customs_delay_days,
                    "production_end_date": production_end.isoformat(),
                    "estimated_port_arrival_date": port_arrival.isoformat(),
                    "estimated_warehouse_date": warehouse_arrival.isoformat(),
                    "elapsed_production_days": elapsed_production_days,
                    "remaining_total_days": max(0, (warehouse_arrival - as_of).days),
                    "production_progress_percent": round(
                        elapsed_production_days / planner.policy.production_days * 100, 1
                    ),
                },
                "estimated_costs": estimated_costs,
            }
        )

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
        "customs_cost_reference": customs_references,
        "freight_reference": freight_history,
        "active_imports": active_import_reports,
        "demand_multiplier": float(demand_multiplier),
        "projected_arrival_date": projected_arrival.isoformat(),
        "products": evaluated,
        "pending_volume_products": missing_volume_risks,
        "purchase_proposal": {
            "status": target_status,
            "items": selected,
            "totals": totals,
            "container_type": container_policy.get("type", "20GP"),
            "container_reference_cbm": float(container_capacity_cbm),
            "container_utilization_percent": round(utilization_percent, 1),
            "container_remaining_cbm": _round(remaining_container_cbm, "0.01"),
            "container_count": int(
                (_decimal(totals.get("total_cbm")) / container_capacity_cbm).to_integral_value(
                    rounding=ROUND_UP
                )
            )
            if totals["total_cbm"]
            else 0,
            "container_equivalent": _round(freight_proration_factor, "0.0001"),
            "total_units": sum(int(row["recommended_units"]) for row in selected),
            "total_cartons": _round(
                sum(
                    (_decimal(row.get("recommended_cartons")) for row in selected),
                    Decimal("0"),
                )
            ),
            "total_skus": len(selected),
            "freight_reference": freight_history.get("summary", {}),
            "freight_full_container_usd": _round(latest_container_freight),
            "freight_proration_factor": _round(freight_proration_factor, "0.0001"),
            "freight_usd_per_cbm": _round(
                latest_container_freight / container_capacity_cbm, "0.0001"
            ),
            "freight_allocation_policy": "proportional_to_used_cbm",
            "required_order_date": min(
                (row["required_order_date"] for row in selected if row["required_order_date"]),
                default=None,
            ),
            "projected_arrival_date": projected_arrival.isoformat(),
            "warnings": warnings,
        },
        "methodology": (
            "Cruce exacto por SKU y, en segundo termino, similitud de nombre. La demanda proviene de ventas Facto; "
            "el volumen y FOB provienen de documentos Chinafore; el flete maritimo usa primero la factura mas "
            "reciente de AD/ADS Cargas almacenada en el CRM por Facto y solo recurre al historico verificado "
            "cuando esa evidencia no permite una valorizacion USD auditable. La tarifa del 20GP completo se "
            "prorratea por los m3 efectivamente usados sobre una capacidad util de 27 m3. Las cantidades se "
            "redondean a cajas completas usando Packing Box (unidades por caja) del catalogo Chinafore. "
            "Seguro, derechos, gastos locales "
            "y honorarios usan como referencia historica las facturas y solicitudes de fondos fechadas de "
            "Agencia Rodriguez Palma encontradas en Gmail, con j.rodriguez@agenciarodriguezpalma.cl como "
            "contacto de referencia. Esos importes son variables por despacho y nunca se tratan como tarifas fijas. "
            "La proforma 26TDC12 se considera mercaderia confirmada en produccion y descuenta necesidad futura, "
            "pero no incrementa el stock disponible antes de su recepcion en bodega."
        ),
    }
