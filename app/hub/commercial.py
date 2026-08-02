"""Commercial customer intelligence built from read-only business sources.

Facto remains the financial source of truth. Tiendanube contributes the web
channel and order history, while the CRM contributes the reviewed HVAC
classification, territory and pipeline state. Exact identifiers are the only
automatic merge keys; fuzzy names are never consolidated automatically.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.hub.chile_geo import canonical_chilean_location
from app.hub.finance import _amounts, _customer, _document_date
from app.hub.inventory import payload_rows


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _text(value))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _rut(value: Any) -> str:
    return re.sub(r"[^0-9kK]", "", _text(value)).upper()


def _email(value: Any) -> str:
    candidate = _text(value).casefold()
    return candidate if "@" in candidate and "." in candidate.rsplit("@", 1)[-1] else ""


def _phone(value: Any) -> str:
    digits = re.sub(r"\D", "", _text(value))
    if digits.startswith("56") and len(digits) >= 11:
        return f"+{digits}"
    if len(digits) == 9:
        return f"+56{digits}"
    return f"+{digits}" if len(digits) >= 8 else ""


def _decimal(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        return Decimal("0")
    candidate = _text(value)
    if "," in candidate:
        candidate = candidate.replace(".", "").replace(",", ".")
    try:
        return Decimal(candidate or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(_text(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(_text(value)[:10])
        except ValueError:
            return None


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _objects(data: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    values = [data]
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            values.append(value)
    return values


def _find(data: dict[str, Any], keys: Iterable[str], *nested: str) -> Any:
    for candidate in _objects(data, *nested):
        value = _first(candidate, *keys)
        if value not in (None, ""):
            return value
    return None


def _identity(data: dict[str, Any], *, fallback: str) -> tuple[str, list[str]]:
    aliases: list[str] = []
    if data.get("tax_id"):
        aliases.append(f"rut:{_rut(data['tax_id'])}")
    if data.get("email"):
        aliases.append(f"email:{_email(data['email'])}")
    if data.get("phone"):
        aliases.append(f"phone:{_phone(data['phone'])}")
    if data.get("name"):
        aliases.append(f"name:{_normalized_text(data['name'])}")
    aliases = [alias for alias in aliases if not alias.endswith(":")]
    preferred = next(
        (alias for alias in aliases if alias.startswith(("rut:", "email:", "phone:"))),
        aliases[0] if aliases else fallback,
    )
    return preferred, aliases


def _blank_customer(customer_key: str) -> dict[str, Any]:
    return {
        "customer_key": customer_key,
        "name": "",
        "legal_name": "",
        "tax_id": "",
        "email": "",
        "phone": "",
        "whatsapp": "",
        "region": "",
        "city": "",
        "address": "",
        "location_source": "",
        "location_verified_at": None,
        "sources": [],
        "facto_net_sales": 0.0,
        "facto_documents": 0,
        "facto_net_sales_by_month": {},
        "tiendanube_gross_sales": 0.0,
        "tiendanube_orders": 0,
        "tiendanube_sales_by_month": {},
        "purchase_months": {},
        "top_products": [],
        "product_families": [],
        "product_history": [],
        "_product_units": {},
        "_product_families": {},
        "_product_history": {},
        "first_purchase_at": None,
        "last_purchase_at": None,
        "source_ids": {},
    }


def _merge_contact(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ("name", "legal_name", "tax_id", "email", "phone"):
        if not target.get(key) and source.get(key):
            target[key] = source[key]

    incoming_has_location = any(source.get(key) for key in ("region", "city", "address"))
    incoming_date = _date(source.get("location_verified_at"))
    current_date = _date(target.get("location_verified_at"))
    incoming_priority = int(source.get("_location_priority") or 0)
    current_priority = int(target.get("_location_priority") or 0)
    should_replace_location = incoming_has_location and (
        not any(target.get(key) for key in ("region", "city", "address"))
        or incoming_priority > current_priority
        or (
            incoming_priority == current_priority
            and incoming_date is not None
            and (current_date is None or incoming_date >= current_date)
        )
    )
    if should_replace_location:
        for key in ("region", "city", "address", "location_source"):
            if source.get(key):
                target[key] = source[key]
        if incoming_date:
            target["location_verified_at"] = incoming_date.isoformat()
        target["_location_priority"] = incoming_priority

    phone = _phone(source.get("phone") or target.get("phone"))
    if phone:
        target["phone"] = phone
        if phone.startswith("+569"):
            target["whatsapp"] = phone
    source_name = _text(source.get("source"))
    if source_name and source_name not in target["sources"]:
        target["sources"].append(source_name)
    source_id = _text(source.get("source_id"))
    if source_name and source_id:
        target["source_ids"][source_name] = source_id


def _facto_contact(row: dict[str, Any], *, source_id: str = "") -> dict[str, Any]:
    candidates = _objects(
        row,
        "customer",
        "client",
        "receiver",
        "recipient",
        "receptor",
        "header",
        "address",
    )

    def find(*keys: str) -> Any:
        for candidate in candidates:
            value = _first(candidate, *keys)
            if value not in (None, ""):
                return value
        return None

    name = find(
        "receiver_legal_name",
        "receiver_business_name",
        "receiver_name",
        "business_name",
        "legal_name",
        "razon_social",
        "name",
        "nombre",
    )
    raw_region = find("receiver_region", "region", "state")
    raw_city = find("receiver_city", "city", "commune", "comuna")
    raw_district = find("receiver_district", "district", "county", "municipality")
    region, city = canonical_chilean_location(
        city=raw_city,
        district=raw_district,
        region=raw_region,
    )
    location_date = _document_date(row)
    is_invoice_location = bool(
        location_date
        or isinstance(row.get("header"), dict)
        or _first(row, "document_id", "issue_date")
    )
    return {
        "name": _text(name),
        "legal_name": _text(find("legal_name", "business_name", "razon_social") or name),
        "tax_id": _rut(
            find(
                "receiver_tax_id",
                "receiver_tax_id_code",
                "tax_id",
                "tax_id_code",
                "rut",
                "RUT",
            )
        ),
        "email": _email(find("email", "receiver_email", "contact_email", "correo")),
        "phone": _phone(
            find("phone", "telephone", "mobile", "receiver_phone", "telefono", "celular")
        ),
        "region": region or _text(raw_region),
        "city": city or _text(raw_city or raw_district),
        "address": _text(
            find(
                "receiver_address",
                "address",
                "street",
                "street_address",
                "direccion",
            )
        ),
        "location_source": "facto_invoice" if is_invoice_location else "facto_customer",
        "location_verified_at": location_date.isoformat() if location_date else None,
        "_location_priority": 20 if is_invoice_location else 10,
        "source": "facto",
        "source_id": source_id
        or _text(find("client_id", "customer_id", "receiver_id", "id")),
    }


def _tiendanube_contact(row: dict[str, Any], *, source_id: str = "") -> dict[str, Any]:
    customer = row.get("customer") if isinstance(row.get("customer"), dict) else row
    billing = (
        row.get("billing_address")
        if isinstance(row.get("billing_address"), dict)
        else customer.get("default_address")
        if isinstance(customer.get("default_address"), dict)
        else {}
    )
    name = _text(
        _first(customer, "name", "contact_name")
        or " ".join(
            part
            for part in (
                _text(customer.get("first_name")),
                _text(customer.get("last_name")),
            )
            if part
        )
    )
    raw_region = _first(billing, "province", "state", "region")
    raw_city = _first(billing, "city", "locality")
    raw_district = _first(billing, "commune", "district")
    region, city = canonical_chilean_location(
        city=raw_city,
        district=raw_district,
        region=raw_region,
    )
    location_date = _date(
        _first(row, "created_at", "updated_at", "completed_at")
        or _first(customer, "created_at", "updated_at")
    )
    return {
        "name": name,
        "legal_name": _text(
            _first(customer, "business_name", "company", "billing_name") or name
        ),
        "tax_id": _rut(
            _first(customer, "identification", "tax_id", "document", "rut")
            or _first(billing, "identification", "tax_id", "document", "rut")
        ),
        "email": _email(_first(customer, "email", "contact_email") or row.get("contact_email")),
        "phone": _phone(
            _first(customer, "phone", "mobile")
            or _first(billing, "phone", "mobile")
            or row.get("contact_phone")
        ),
        "region": region or _text(raw_region),
        "city": city or _text(raw_district or raw_city),
        "address": _text(
            _first(billing, "address", "street", "address1", "street_address")
        ),
        "location_source": "tiendanube_order" if location_date else "tiendanube_customer",
        "location_verified_at": location_date.isoformat() if location_date else None,
        "_location_priority": 20 if location_date else 10,
        "source": "tiendanube",
        "source_id": source_id or _text(_first(customer, "id", "customer_id")),
    }


def _upsert_customer(
    customers: dict[str, dict[str, Any]],
    alias_index: dict[str, str],
    contact: dict[str, Any],
    *,
    fallback: str,
) -> dict[str, Any]:
    preferred, aliases = _identity(contact, fallback=fallback)
    exact_aliases = [
        alias for alias in aliases if alias.startswith(("rut:", "email:", "phone:"))
    ]
    key = next((alias_index[alias] for alias in exact_aliases if alias in alias_index), "")
    if not key:
        key = preferred
    target = customers.setdefault(key, _blank_customer(key))
    _merge_contact(target, contact)
    for alias in aliases:
        if alias.startswith(("rut:", "email:", "phone:")):
            alias_index[alias] = key
    return target


def _record_purchase(
    target: dict[str, Any],
    purchase_date: date | None,
    *,
    source: str,
    amount: Decimal,
) -> None:
    if purchase_date is None:
        return
    first = _date(target.get("first_purchase_at"))
    last = _date(target.get("last_purchase_at"))
    target["first_purchase_at"] = min(first, purchase_date).isoformat() if first else purchase_date.isoformat()
    target["last_purchase_at"] = max(last, purchase_date).isoformat() if last else purchase_date.isoformat()
    month = purchase_date.strftime("%Y-%m")
    purchase_months = target.setdefault("purchase_months", {})
    purchase_months[month] = int(purchase_months.get(month, 0)) + 1
    amount_key = (
        "facto_net_sales_by_month"
        if source == "facto"
        else "tiendanube_sales_by_month"
    )
    amounts = target.setdefault(amount_key, {})
    amounts[month] = float(Decimal(str(amounts.get(month, 0))) + amount)


def _line_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload_rows(
        payload,
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
        "document_details",
        "documentDetails",
        "product_details",
        "productDetails",
        "rows",
        "concepts",
        "conceptos",
    )


def _product_family(value: Any) -> str:
    name = _normalized_text(value)
    families = (
        (
            "Bombas de condensado",
            ("bomba condensado", "bomba de condensado", "condensate pump"),
        ),
        (
            "Bombas de vacio",
            ("bomba vacio", "bomba de vacio", "vacuum pump"),
        ),
        (
            "Herramientas HVAC",
            (
                "manifold",
                "abocardador",
                "expandidor",
                "torquimetro",
                "herramient",
                "detector fuga",
                "vacuometro",
            ),
        ),
        (
            "Refrigeración",
            (
                "refriger",
                "gas r32",
                "gas r410",
                "gas r134",
                "valvula solenoide",
                "compresor",
                "filtro secador",
            ),
        ),
        (
            "Tuberías y conexiones",
            (
                "tubo cobre",
                "tuberia",
                "tuerca flare",
                "union",
                "codo",
                "tee ",
                "conector",
                "flare",
            ),
        ),
        (
            "Instalación y montaje",
            (
                "soporte",
                "canaleta",
                "cinta",
                "aislacion",
                "aislante",
                "drenaje",
                "manguera",
            ),
        ),
        (
            "Ventilación",
            ("ventilador", "extractor", "rejilla", "difusor", "ventilacion"),
        ),
        (
            "Equipos de climatización",
            (
                "split",
                "aire acondicionado",
                "climatizador",
                "fan coil",
                "fancoil",
                "cassette",
                "piso cielo",
            ),
        ),
    )
    for family, keywords in families:
        if any(keyword in name for keyword in keywords):
            return family
    return "Otros productos HVAC"


def _record_product_activity(
    target: dict[str, Any],
    payload: dict[str, Any],
    *,
    purchase_date: date | None = None,
    source: str = "",
    document_id: str = "",
) -> None:
    product_units = target.setdefault("_product_units", {})
    family_units = target.setdefault("_product_families", {})
    product_history = target.setdefault("_product_history", {})
    for line in _line_rows(payload):
        product_name = _text(
            _find(
                line,
                (
                    "line_description",
                    "item_description",
                    "product_description",
                    "productDescription",
                    "name",
                    "nombre",
                    "product_name",
                    "description",
                    "descripcion",
                    "detalle",
                    "title",
                    "glosa",
                    "concept",
                    "concepto",
                ),
                "product",
                "item",
            )
        )
        quantity = _decimal(
            _find(
                line,
                ("quantity", "qty", "cantidad", "units", "unidades"),
                "product",
                "item",
            )
            or 1
        )
        if quantity <= 0:
            quantity = Decimal("1")
        sku = _text(
            _find(
                line,
                (
                    "sku",
                    "product_sku",
                    "productSku",
                    "item_sku",
                    "code",
                    "product_code",
                    "codigo",
                    "codigo_producto",
                    "codigoProducto",
                    "item_code",
                    "reference",
                ),
                "product",
                "item",
            )
        )
        source_product_id = _text(
            _find(
                line,
                ("product_id", "productId", "id_producto", "producto_id", "id"),
                "product",
                "item",
            )
        )
        if not product_name:
            product_name = sku or source_product_id
        if not product_name:
            continue
        product_units[product_name] = float(
            Decimal(str(product_units.get(product_name, 0))) + quantity
        )
        family = _product_family(product_name)
        family_units[family] = float(
            Decimal(str(family_units.get(family, 0))) + quantity
        )
        history_key = f"sku:{_normalized_text(sku)}" if _normalized_text(sku) else (
            f"product:{_normalized_text(source_product_id)}"
            if _normalized_text(source_product_id)
            else f"name:{_normalized_text(product_name)}"
        )
        history = product_history.setdefault(
            history_key,
            {
                "sku": sku,
                "source_product_id": source_product_id,
                "name": product_name,
                "units": 0.0,
                "purchase_events": 0,
                "first_purchase_at": None,
                "last_purchase_at": None,
                "sources": [],
                "_document_ids": [],
            },
        )
        history["units"] = float(Decimal(str(history.get("units", 0))) + quantity)
        if not history.get("sku") and sku:
            history["sku"] = sku
        if not history.get("source_product_id") and source_product_id:
            history["source_product_id"] = source_product_id
        if not history.get("name") and product_name:
            history["name"] = product_name
        event_key = f"{source}:{document_id}" if document_id else ""
        observed_documents = history.setdefault("_document_ids", [])
        if not event_key or event_key not in observed_documents:
            history["purchase_events"] = int(history.get("purchase_events", 0)) + 1
            if event_key:
                observed_documents.append(event_key)
        if source and source not in history["sources"]:
            history["sources"].append(source)
        if purchase_date:
            first_purchase = _date(history.get("first_purchase_at"))
            last_purchase = _date(history.get("last_purchase_at"))
            history["first_purchase_at"] = (
                min(first_purchase, purchase_date).isoformat()
                if first_purchase
                else purchase_date.isoformat()
            )
            history["last_purchase_at"] = (
                max(last_purchase, purchase_date).isoformat()
                if last_purchase
                else purchase_date.isoformat()
            )


def extract_commercial_snapshot(
    facto_customers_payload: Any,
    facto_documents_payload: Any,
    tiendanube_customers_payload: Any,
    tiendanube_orders_payload: Any,
    *,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Return one compact, exact-match customer portfolio snapshot."""

    today = as_of or datetime.now(UTC).date()
    customers: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}

    for index, row in enumerate(
        payload_rows(facto_customers_payload, "clients", "customers", "data", "items")
    ):
        source_id = _text(_first(row, "id", "client_id", "customer_id"))
        _upsert_customer(
            customers,
            aliases,
            _facto_contact(row, source_id=source_id),
            fallback=f"facto-client:{source_id or index}",
        )

    for index, document in enumerate(
        payload_rows(facto_documents_payload, "documents", "data", "items")
    ):
        document_id = _text(_first(document, "document_id", "id"))
        name, tax_id = _customer(document)
        contact = _facto_contact(document)
        contact["name"] = contact.get("name") or name
        contact["legal_name"] = contact.get("legal_name") or name
        contact["tax_id"] = contact.get("tax_id") or _rut(tax_id)
        target = _upsert_customer(
            customers,
            aliases,
            contact,
            fallback=f"facto-document:{document_id or index}",
        )
        net, _, _ = _amounts(document)
        target["facto_net_sales"] = float(
            Decimal(str(target["facto_net_sales"])) + net
        )
        target["facto_documents"] += 1
        purchase_date = _document_date(document)
        _record_purchase(
            target,
            purchase_date,
            source="facto",
            amount=net,
        )
        _record_product_activity(
            target,
            document,
            purchase_date=purchase_date,
            source="facto",
            document_id=document_id,
        )

    for index, row in enumerate(
        payload_rows(tiendanube_customers_payload, "customers", "data", "items")
    ):
        source_id = _text(_first(row, "id", "customer_id"))
        _upsert_customer(
            customers,
            aliases,
            _tiendanube_contact(row, source_id=source_id),
            fallback=f"tiendanube-customer:{source_id or index}",
        )

    for index, order in enumerate(
        payload_rows(tiendanube_orders_payload, "orders", "data", "items")
    ):
        order_id = _text(_first(order, "id", "order_id", "number"))
        target = _upsert_customer(
            customers,
            aliases,
            _tiendanube_contact(order),
            fallback=f"tiendanube-order:{order_id or index}",
        )
        target["tiendanube_gross_sales"] = float(
            Decimal(str(target["tiendanube_gross_sales"]))
            + _decimal(_first(order, "total", "total_amount", "subtotal"))
        )
        target["tiendanube_orders"] += 1
        purchase_date = _date(
            _first(order, "completed_at", "paid_at", "created_at", "date")
        )
        _record_purchase(
            target,
            purchase_date,
            source="tiendanube",
            amount=_decimal(_first(order, "total", "total_amount", "subtotal")),
        )
        _record_product_activity(
            target,
            order,
            purchase_date=purchase_date,
            source="tiendanube",
            document_id=order_id,
        )

    rows: list[dict[str, Any]] = []
    for customer in customers.values():
        product_units = customer.pop("_product_units", {})
        family_units = customer.pop("_product_families", {})
        product_history = customer.pop("_product_history", {})
        customer.pop("_location_priority", None)
        customer["top_products"] = [
            {"name": name, "units": units}
            for name, units in sorted(
                product_units.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:5]
        ]
        customer["product_families"] = [
            {"name": name, "units": units}
            for name, units in sorted(
                family_units.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:5]
        ]
        customer["product_history"] = [
            {
                key: value
                for key, value in history.items()
                if key != "_document_ids"
            }
            for history in sorted(
                product_history.values(),
                key=lambda item: (
                    _date(item.get("last_purchase_at")) or date.min,
                    Decimal(str(item.get("units", 0))),
                ),
                reverse=True,
            )
        ]
        sources = sorted(customer["sources"])
        last_purchase = _date(customer.get("last_purchase_at"))
        first_purchase = _date(customer.get("first_purchase_at"))
        activity_count = int(customer["facto_documents"]) + int(customer["tiendanube_orders"])
        days_since_purchase = (today - last_purchase).days if last_purchase else None
        if first_purchase and (today - first_purchase).days <= 60 and activity_count <= 2:
            lifecycle = "new"
        elif days_since_purchase is None:
            lifecycle = "no_purchase"
        elif days_since_purchase <= 90:
            lifecycle = "active"
        elif days_since_purchase <= 180:
            lifecycle = "at_risk"
        else:
            lifecycle = "dormant"
        source_channel = (
            "both"
            if {"facto", "tiendanube"}.issubset(sources)
            else "tiendanube_only"
            if "tiendanube" in sources
            else "facto_only"
        )
        rows.append(
            {
                **customer,
                "sources": sources,
                "source_channel": source_channel,
                "lifecycle": lifecycle,
                "days_since_purchase": days_since_purchase,
                "contactable": bool(customer.get("email") or customer.get("phone")),
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            Decimal(str(item.get("facto_net_sales", 0))),
            int(item.get("facto_documents", 0)) + int(item.get("tiendanube_orders", 0)),
        ),
        reverse=True,
    )


def _customer_product_opportunities(
    customers: list[dict[str, Any]],
    inventory_snapshot: list[dict[str, Any]],
    *,
    as_of: date,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Correlate exact customer purchase history with current inventory.

    A warehouse entry date is not available in the current Facto payload. The
    result therefore reports days without an observed sale, never invented
    days in storage.
    """

    inventory_by_sku: dict[str, dict[str, Any]] = {}
    inventory_by_source_product_id: dict[str, dict[str, Any]] = {}
    inventory_by_name: dict[str, dict[str, Any]] = {}
    normalized_inventory: list[tuple[str, dict[str, Any]]] = []
    inventory_by_family: dict[str, list[dict[str, Any]]] = {}
    for item in inventory_snapshot:
        if not isinstance(item, dict):
            continue
        sku_key = _normalized_text(item.get("sku"))
        source_product_id_key = _normalized_text(item.get("source_product_id"))
        name_key = _normalized_text(item.get("name"))
        if sku_key:
            inventory_by_sku[sku_key] = item
        if source_product_id_key:
            inventory_by_source_product_id[source_product_id_key] = item
        if name_key:
            inventory_by_name[name_key] = item
            normalized_inventory.append((name_key, item))
            family = _product_family(item.get("name"))
            if family != "Otros productos HVAC" and item.get("stock_known"):
                if _decimal(item.get("available_units")) > 0:
                    inventory_by_family.setdefault(family, []).append(item)

    opportunities: list[dict[str, Any]] = []
    diagnostics = {
        "customers_reviewed": len(customers),
        "customers_with_product_history": 0,
        "customers_using_legacy_top_products": 0,
        "purchase_products_reviewed": 0,
        "inventory_products_reviewed": len(inventory_snapshot),
        "matched_customer_products": 0,
        "family_matches": 0,
        "purchase_products_without_inventory_match": 0,
        "matched_products_without_stock": 0,
        "matched_products_out_of_stock": 0,
        "matched_products_without_purchase_date": 0,
        "matched_products_recent_purchase": 0,
        "eligible_opportunities": 0,
    }
    for customer in customers:
        product_history = [
            item for item in customer.get("product_history", []) if isinstance(item, dict)
        ]
        purchase_recency_scope = "product"
        if product_history:
            diagnostics["customers_with_product_history"] += 1
        else:
            product_history = [
                {
                    **item,
                    "purchase_events": item.get("purchase_events")
                    or item.get("documents")
                    or 1,
                    "last_purchase_at": customer.get("last_purchase_at"),
                    "first_purchase_at": customer.get("first_purchase_at"),
                    "sources": customer.get("sources", []),
                }
                for item in customer.get("top_products", [])
                if isinstance(item, dict)
            ]
            if product_history:
                diagnostics["customers_using_legacy_top_products"] += 1
                purchase_recency_scope = "customer_proxy"

        diagnostics["purchase_products_reviewed"] += len(product_history)
        for history in product_history:
            if not isinstance(history, dict):
                continue
            sku_key = _normalized_text(history.get("sku"))
            source_product_id_key = _normalized_text(history.get("source_product_id"))
            name_key = _normalized_text(history.get("name"))
            inventory = inventory_by_sku.get(sku_key) if sku_key else None
            match_method = "exact_sku" if inventory is not None else ""
            if inventory is None and source_product_id_key:
                inventory = inventory_by_source_product_id.get(source_product_id_key)
                match_method = "exact_product_id" if inventory is not None else ""
            if inventory is None and name_key:
                inventory = inventory_by_name.get(name_key)
                match_method = "exact_normalized_name" if inventory is not None else ""
            if inventory is None and name_key and len(name_key) >= 8:
                containment_matches = [
                    item
                    for inventory_name, item in normalized_inventory
                    if len(inventory_name) >= 8
                    and (name_key in inventory_name or inventory_name in name_key)
                ]
                if len(containment_matches) == 1:
                    inventory = containment_matches[0]
                    match_method = "unique_name_containment"
            historical_product_name = _text(history.get("name"))
            product_family = _product_family(historical_product_name)
            if inventory is None and product_family != "Otros productos HVAC":
                family_candidates = inventory_by_family.get(product_family, [])
                if family_candidates:
                    inventory = max(
                        family_candidates,
                        key=lambda item: (
                            _decimal(item.get("stock_value")),
                            _decimal(item.get("available_units")),
                        ),
                    )
                    match_method = "product_family"
                    diagnostics["family_matches"] += 1
            if inventory is None:
                diagnostics["purchase_products_without_inventory_match"] += 1
                continue
            diagnostics["matched_customer_products"] += 1
            if not inventory.get("stock_known"):
                diagnostics["matched_products_without_stock"] += 1
                continue

            available_units = _decimal(inventory.get("available_units"))
            last_customer_purchase = _date(history.get("last_purchase_at"))
            if available_units <= 0:
                diagnostics["matched_products_out_of_stock"] += 1
                continue
            if last_customer_purchase is None:
                diagnostics["matched_products_without_purchase_date"] += 1
                continue
            customer_lapse = max(0, (as_of - last_customer_purchase).days)
            if customer_lapse < 90:
                diagnostics["matched_products_recent_purchase"] += 1
                continue

            last_product_sale = _date(inventory.get("last_sale_at"))
            history_start = _date(inventory.get("sales_history_start"))
            days_without_sale: int | None = None
            inactivity_is_minimum = False
            if last_product_sale:
                days_without_sale = max(0, (as_of - last_product_sale).days)
            elif inventory.get("sales_history_available") and history_start:
                days_without_sale = max(0, (as_of - history_start).days)
                inactivity_is_minimum = True

            historical_units = float(_decimal(history.get("units")))
            purchase_events = int(history.get("purchase_events", 0) or 0)
            score = min(30, round(customer_lapse / 12))
            score += min(25, purchase_events * 5 + round(min(historical_units, 50) / 5))
            score += min(20, 5 + round(min(float(available_units), 150) / 10))
            if days_without_sale is not None:
                score += 20 if days_without_sale >= 180 else 12 if days_without_sale >= 90 else 6 if days_without_sale >= 60 else 0
            if customer.get("contactable"):
                score += 5
            score = min(100, score)
            priority = "high" if score >= 65 else "medium" if score >= 45 else "normal"

            product_name = _text(inventory.get("name") or history.get("name"))
            customer_name = customer.get("name") or customer.get("legal_name") or "El cliente"
            if match_method == "product_family":
                reason_parts = [
                    f"{customer_name} compró antes productos de la familia {product_family}",
                    f"({historical_product_name}) y lleva {customer_lapse} días sin recomprar esa familia.",
                    f"Hoy hay stock de {product_name} para preparar una propuesta relacionada.",
                ]
            elif purchase_recency_scope == "product":
                reason_parts = [
                    f"{customer_name} compró {product_name}",
                    f"y su última compra de este producto fue hace {customer_lapse} días.",
                ]
            else:
                reason_parts = [
                    f"{customer_name} compró {product_name} en su historial disponible.",
                    f"La última compra observada del cliente fue hace {customer_lapse} días;",
                    "el análisis histórico no conservó una fecha específica por producto.",
                ]
            reason_parts.append(
                f"Actualmente hay {float(available_units):g} unidades disponibles."
            )
            if days_without_sale is not None:
                minimum_label = "al menos " if inactivity_is_minimum else ""
                reason_parts.append(
                    f"El producto lleva {minimum_label}{days_without_sale} días sin venta observada."
                )

            unit_cost = _decimal(inventory.get("unit_cost_source"))
            opportunities.append(
                {
                    "customer_key": customer.get("customer_key"),
                    "crm_company_id": customer.get("crm_company_id"),
                    "customer_name": customer.get("name") or customer.get("legal_name"),
                    "tax_id": customer.get("tax_id"),
                    "email": customer.get("email"),
                    "phone": customer.get("phone"),
                    "whatsapp": customer.get("whatsapp"),
                    "product_name": product_name,
                    "historical_product_name": historical_product_name,
                    "product_family": product_family,
                    "sku": inventory.get("sku") or history.get("sku"),
                    "historical_units": historical_units,
                    "purchase_events": purchase_events,
                    "customer_last_purchase_at": last_customer_purchase.isoformat(),
                    "days_since_customer_product_purchase": customer_lapse,
                    "available_units": float(available_units),
                    "product_last_sale_at": last_product_sale.isoformat() if last_product_sale else None,
                    "days_without_product_sale": days_without_sale,
                    "inactivity_is_minimum": inactivity_is_minimum,
                    "stock_value": float(available_units * unit_cost),
                    "cost_currency_code": inventory.get("cost_currency_code"),
                    "score": score,
                    "priority": priority,
                    "reason": " ".join(reason_parts),
                    "purchase_recency_scope": purchase_recency_scope,
                    "inventory_match_method": match_method,
                    "evidence": {
                        "purchase_sources": history.get("sources", []),
                        "product_purchase_date_available": purchase_recency_scope == "product",
                        "inventory_source": inventory.get("source"),
                        "sales_history_start": inventory.get("sales_history_start"),
                        "sales_history_end": inventory.get("sales_history_end"),
                    },
                }
            )

    ranked = sorted(
        opportunities,
        key=lambda item: (
            int(item.get("score", 0)),
            int(item.get("days_since_customer_product_purchase", 0)),
            float(item.get("stock_value", 0)),
        ),
        reverse=True,
    )[:100]
    diagnostics["eligible_opportunities"] = len(ranked)
    return ranked, diagnostics


def build_commercial_report(
    commercial_snapshot: list[dict[str, Any]],
    crm_companies: list[dict[str, Any]],
    financial_snapshot: dict[str, Any] | None = None,
    inventory_snapshot: list[dict[str, Any]] | None = None,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Enrich the provider portfolio with reviewed CRM classifications."""

    today = as_of or datetime.now(UTC).date()
    customers = [dict(row) for row in commercial_snapshot if isinstance(row, dict)]
    alias_index: dict[str, int] = {}
    for index, customer in enumerate(customers):
        _, aliases = _identity(customer, fallback=customer.get("customer_key", f"row:{index}"))
        for alias in aliases:
            if alias.startswith(("rut:", "email:", "phone:")):
                alias_index[alias] = index

    for company_index, company in enumerate(crm_companies):
        if not isinstance(company, dict):
            continue
        crm_contact = {
            "name": _text(company.get("name")),
            "legal_name": _text(company.get("legal_name") or company.get("legalName")),
            "tax_id": _rut(company.get("rut")),
            "email": _email(company.get("email")),
            "phone": _phone(
                company.get("whatsapp")
                or company.get("whatsapp_number")
                or company.get("whatsappNumber")
                or company.get("phone")
            ),
            "region": _text(company.get("region")),
            "city": _text(company.get("city")),
            "address": _text(company.get("address")),
            "location_source": "crm_reviewed",
            "_location_priority": 100,
        }
        _, company_aliases = _identity(
            crm_contact,
            fallback=f"crm:{company.get('id') or company_index}",
        )
        match_index = next(
            (
                alias_index[alias]
                for alias in company_aliases
                if alias.startswith(("rut:", "email:", "phone:")) and alias in alias_index
            ),
            None,
        )
        if match_index is None:
            customer = _blank_customer(f"crm:{company.get('id') or company_index}")
            _merge_contact(customer, {**crm_contact, "source": "crm", "source_id": company.get("id")})
            customer.update(
                {
                    "source_channel": "crm_only",
                    "lifecycle": "no_purchase",
                    "days_since_purchase": None,
                    "contactable": bool(crm_contact["email"] or crm_contact["phone"]),
                }
            )
            customers.append(customer)
            match_index = len(customers) - 1
            for alias in company_aliases:
                if alias.startswith(("rut:", "email:", "phone:")):
                    alias_index[alias] = match_index

        target = customers[match_index]
        target["crm_company_id"] = company.get("id")
        target["crm_type"] = _text(company.get("type") or "otro")
        target["crm_status"] = _text(company.get("status") or "prospecto")
        target["crm_priority"] = _text(company.get("priority") or "media")
        _merge_contact(
            target,
            {
                **crm_contact,
                "source": "crm",
                "source_id": company.get("id"),
            },
        )
        target["source"] = _text(company.get("source"))
        if "crm" not in target["sources"]:
            target["sources"].append("crm")

    for row in customers:
        row.pop("_product_units", None)
        row.pop("_product_families", None)
        row.pop("_location_priority", None)
        row["sources"] = sorted(set(row.get("sources", [])))
        if row.get("source_channel") not in {
            "facto_only",
            "tiendanube_only",
            "both",
        }:
            row["source_channel"] = (
                "both"
                if {"facto", "tiendanube"}.issubset(row["sources"])
                else "tiendanube_only"
                if "tiendanube" in row["sources"]
                else "facto_only"
                if "facto" in row["sources"]
                else "crm_only"
            )
        row["contactable"] = bool(
            row.get("email") or row.get("whatsapp") or row.get("phone")
        )
        row["email_ready"] = bool(row.get("email"))
        row["whatsapp_ready"] = bool(row.get("whatsapp"))
        row["purchase_events"] = int(row.get("facto_documents", 0)) + int(
            row.get("tiendanube_orders", 0)
        )
        row["average_net_ticket"] = (
            float(
                Decimal(str(row.get("facto_net_sales", 0)))
                / Decimal(str(row["facto_documents"]))
            )
            if int(row.get("facto_documents", 0))
            else 0.0
        )
        row["commercial_value"] = float(
            Decimal(str(row.get("facto_net_sales", 0)))
            or (
                Decimal(str(row.get("tiendanube_gross_sales", 0)))
                / Decimal("1.19")
            )
        )

    ranked = sorted(
        (
            (index, Decimal(str(row.get("commercial_value", 0))))
            for index, row in enumerate(customers)
            if Decimal(str(row.get("commercial_value", 0))) > 0
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    value_position = {index: position for position, (index, _) in enumerate(ranked)}
    action_labels = {
        "rescue_priority": "Recuperar cliente valioso",
        "convert_web_to_b2b": "Desarrollar comprador web",
        "reactivate": "Reactivar cliente",
        "onboard": "Acompañar cliente nuevo",
        "loyalty_cross_sell": "Fidelizar y ampliar mix",
        "complete_contact": "Completar datos de contacto",
        "qualify": "Calificar prospecto",
        "follow_up": "Seguimiento comercial",
    }
    opportunity_counts: Counter[str] = Counter()

    for index, row in enumerate(customers):
        position = value_position.get(index)
        positive_count = max(1, len(ranked))
        value_percentile = (
            1 - (position / max(1, positive_count - 1))
            if position is not None
            else 0
        )
        value_points = round(value_percentile * 40)
        lifecycle = _text(row.get("lifecycle"))
        lifecycle_points = {
            "new": 22,
            "active": 25,
            "at_risk": 13,
            "dormant": 4,
            "no_purchase": 0,
        }.get(lifecycle, 0)
        frequency_points = min(20, int(row.get("purchase_events", 0)) * 3)
        source_points = 10 if row.get("source_channel") == "both" else 4
        contact_points = 5 if row.get("contactable") else 0
        score = min(
            100,
            value_points
            + lifecycle_points
            + frequency_points
            + source_points
            + contact_points,
        )
        row["commercial_score"] = score
        row["value_tier"] = (
            "A"
            if score >= 75
            else "B"
            if score >= 55
            else "C"
            if score >= 35
            else "D"
        )

        if not row.get("contactable"):
            action = "complete_contact"
            priority = "high"
        elif row.get("source_channel") == "tiendanube_only":
            action = "convert_web_to_b2b"
            priority = "high" if row["purchase_events"] >= 2 else "medium"
        elif lifecycle in {"at_risk", "dormant"} and row["value_tier"] in {"A", "B"}:
            action = "rescue_priority"
            priority = "urgent"
        elif lifecycle == "dormant":
            action = "reactivate"
            priority = "high"
        elif lifecycle == "new":
            action = "onboard"
            priority = "medium"
        elif lifecycle == "active" and row["purchase_events"] >= 4:
            action = "loyalty_cross_sell"
            priority = "high" if row["value_tier"] in {"A", "B"} else "medium"
        elif lifecycle == "no_purchase":
            action = "qualify"
            priority = "medium"
        else:
            action = "follow_up"
            priority = "normal"
        row["recommended_action"] = action
        row["recommended_action_label"] = action_labels[action]
        row["opportunity_priority"] = priority
        opportunity_counts[priority] += 1

    customer_product_opportunities, product_opportunity_diagnostics = (
        _customer_product_opportunities(
            customers,
            inventory_snapshot or [],
            as_of=today,
        )
    )

    source_counts = Counter(_text(row.get("source_channel")) for row in customers)
    lifecycle_counts = Counter(_text(row.get("lifecycle")) for row in customers)
    type_counts = Counter(_text(row.get("crm_type") or "sin_clasificar") for row in customers)
    region_counts = Counter(_text(row.get("region") or "Sin region") for row in customers)
    contactable = sum(1 for row in customers if row.get("contactable"))
    total_net_sales = sum(
        (Decimal(str(row.get("facto_net_sales", 0))) for row in customers),
        Decimal("0"),
    )

    segments: list[dict[str, Any]] = []

    def segment(
        segment_id: str,
        name: str,
        reason: str,
        predicate,
        *,
        channel: str = "email",
        priority: str = "medium",
        filters: dict[str, Any] | None = None,
    ) -> None:
        candidates = [row for row in customers if row.get("contactable") and predicate(row)]
        if not candidates:
            return
        segments.append(
            {
                "id": segment_id,
                "name": name,
                "reason": reason,
                "channel": channel,
                "priority": priority,
                "count": len(candidates),
                "email_count": sum(1 for row in candidates if row.get("email_ready")),
                "whatsapp_count": sum(
                    1 for row in candidates if row.get("whatsapp_ready")
                ),
                "filters": filters or {},
                "customer_keys": [row.get("customer_key") for row in candidates[:500]],
                "company_ids": [
                    row.get("crm_company_id")
                    for row in candidates
                    if row.get("crm_company_id")
                ][:500],
            }
        )

    segment(
        "valuable_customers_to_rescue",
        "Clientes valiosos para recuperar",
        "Cartera A/B con más de 90 días sin compra; requiere contacto personal antes de una campaña masiva.",
        lambda row: row.get("recommended_action") == "rescue_priority",
        priority="urgent",
        filters={"recommended_action": "rescue_priority"},
    )
    segment(
        "web_customers_to_develop",
        "Compradores web para desarrollar",
        "Compraron en Climactiva.cl y aún no tienen venta Facto vinculada; oportunidad de convertirlos en clientes recurrentes.",
        lambda row: row.get("source_channel") == "tiendanube_only",
        priority="high",
        filters={"source_channel": "tiendanube_only"},
    )
    segment(
        "loyal_customers_cross_sell",
        "Clientes recurrentes para ampliar mix",
        "Clientes activos con cuatro o más compras observadas y potencial de venta cruzada HVAC.",
        lambda row: row.get("recommended_action") == "loyalty_cross_sell",
        priority="high",
        filters={"recommended_action": "loyalty_cross_sell"},
    )
    segment(
        "new_customer_onboarding",
        "Bienvenida y segunda compra",
        "Clientes nuevos con contacto disponible; conviene acompañarlos hacia una segunda compra.",
        lambda row: row.get("recommended_action") == "onboard",
        filters={"recommended_action": "onboard"},
    )
    segment(
        "dormant_customers",
        "Clientes inactivos para reactivacion",
        "Tienen historial real, contacto disponible y mas de 180 dias sin compra.",
        lambda row: row.get("lifecycle") == "dormant",
        filters={"lifecycle": "dormant"},
    )
    segment(
        "at_risk_customers",
        "Clientes en riesgo",
        "Llevan entre 91 y 180 dias sin compra y requieren revision comercial.",
        lambda row: row.get("lifecycle") == "at_risk",
        priority="high",
        filters={"lifecycle": "at_risk"},
    )
    segment(
        "hvac_technicians",
        "Tecnicos e instaladores",
        "Clasificacion HVAC revisada en el CRM para campanas tecnicas.",
        lambda row: row.get("crm_type") in {"tecnico", "instalador grande"},
        channel="whatsapp",
        filters={"crm_type": ["tecnico", "instalador grande"]},
    )
    segment(
        "hvac_distribution",
        "Distribuidores y tiendas HVAC",
        "Cartera clasificada como distribuidor o tienda comercial para propuestas por volumen.",
        lambda row: row.get("crm_type") in {"distribuidor", "tienda comercial"},
        priority="high",
        filters={"crm_type": ["distribuidor", "tienda comercial"]},
    )

    by_month: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {"new_customers": 0, "returning_customers": 0}
    )
    for row in customers:
        first = _date(row.get("first_purchase_at"))
        if first:
            by_month[first.strftime("%Y-%m")]["new_customers"] += 1
        for month in sorted(row.get("purchase_months", {})):
            if first and month != first.strftime("%Y-%m"):
                by_month[month]["returning_customers"] += 1

    priority_order = {"urgent": 0, "high": 1, "medium": 2, "normal": 3}
    top_opportunities = [
        {
            "customer_key": row.get("customer_key"),
            "crm_company_id": row.get("crm_company_id"),
            "name": row.get("name") or row.get("legal_name"),
            "tax_id": row.get("tax_id"),
            "source_channel": row.get("source_channel"),
            "lifecycle": row.get("lifecycle"),
            "value_tier": row.get("value_tier"),
            "commercial_score": row.get("commercial_score"),
            "commercial_value": row.get("commercial_value"),
            "purchase_events": row.get("purchase_events"),
            "average_net_ticket": row.get("average_net_ticket"),
            "recommended_action": row.get("recommended_action"),
            "recommended_action_label": row.get("recommended_action_label"),
            "opportunity_priority": row.get("opportunity_priority"),
            "last_purchase_at": row.get("last_purchase_at"),
            "email_ready": row.get("email_ready"),
            "whatsapp_ready": row.get("whatsapp_ready"),
            "top_products": row.get("top_products", []),
            "product_families": row.get("product_families", []),
        }
        for row in sorted(
            customers,
            key=lambda item: (
                priority_order.get(_text(item.get("opportunity_priority")), 9),
                -int(item.get("commercial_score", 0)),
                -float(item.get("commercial_value", 0)),
            ),
        )[:30]
    ]

    def ranking_row(row: dict[str, Any], *, source: str) -> dict[str, Any]:
        return {
            "customer_key": row.get("customer_key"),
            "crm_company_id": row.get("crm_company_id"),
            "name": row.get("name") or row.get("legal_name"),
            "legal_name": row.get("legal_name"),
            "tax_id": row.get("tax_id"),
            "email": row.get("email"),
            "phone": row.get("phone"),
            "whatsapp": row.get("whatsapp"),
            "region": row.get("region"),
            "city": row.get("city"),
            "sources": row.get("sources", []),
            "source_channel": row.get("source_channel"),
            "lifecycle": row.get("lifecycle"),
            "last_purchase_at": row.get("last_purchase_at"),
            "commercial_score": row.get("commercial_score"),
            "value_tier": row.get("value_tier"),
            "recommended_action": row.get("recommended_action"),
            "opportunity_priority": row.get("opportunity_priority"),
            "documents": int(row.get("facto_documents", 0))
            if source == "facto"
            else int(row.get("tiendanube_orders", 0)),
            "gross_sales": float(row.get("tiendanube_gross_sales", 0))
            if source == "tiendanube"
            else 0.0,
            "net_sales": float(row.get("facto_net_sales", 0))
            if source == "facto"
            else float(
                Decimal(str(row.get("tiendanube_gross_sales", 0)))
                / Decimal("1.19")
            ),
        }

    facto_ranking = [
        ranking_row(row, source="facto")
        for row in sorted(
            (row for row in customers if "facto" in row.get("sources", [])),
            key=lambda item: (
                Decimal(str(item.get("facto_net_sales", 0))),
                int(item.get("facto_documents", 0)),
            ),
            reverse=True,
        )
    ]
    tiendanube_ranking = [
        ranking_row(row, source="tiendanube")
        for row in sorted(
            (row for row in customers if "tiendanube" in row.get("sources", [])),
            key=lambda item: (
                Decimal(str(item.get("tiendanube_gross_sales", 0))),
                int(item.get("tiendanube_orders", 0)),
            ),
            reverse=True,
        )
    ]

    sales_products = []
    if isinstance(financial_snapshot, dict):
        sales_products = [
            {
                "name": item.get("name"),
                "sku": item.get("sku"),
                "units": float(item.get("units", 0) or 0),
                "net_sales": float(item.get("net_sales_observed", 0) or 0),
            }
            for item in financial_snapshot.get("top_products", [])
            if isinstance(item, dict)
        ]
    if not sales_products:
        observed_products: defaultdict[str, Decimal] = defaultdict(Decimal)
        for row in customers:
            for item in row.get("top_products", []):
                if isinstance(item, dict) and _text(item.get("name")):
                    observed_products[_text(item["name"])] += _decimal(item.get("units"))
        sales_products = [
            {"name": name, "sku": "", "units": float(units), "net_sales": 0.0}
            for name, units in sorted(
                observed_products.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "customers": sorted(
            customers,
            key=lambda item: Decimal(str(item.get("facto_net_sales", 0))),
            reverse=True,
        ),
        "metrics": {
            "customers": len(customers),
            "contactable": contactable,
            "email_ready": sum(1 for row in customers if row.get("email_ready")),
            "whatsapp_ready": sum(
                1 for row in customers if row.get("whatsapp_ready")
            ),
            "facto_net_sales": float(total_net_sales),
            "facto_customers": sum(
                1 for row in customers if "facto" in row.get("sources", [])
            ),
            "tiendanube_customers": sum(
                1 for row in customers if "tiendanube" in row.get("sources", [])
            ),
            "crm_companies": sum(1 for row in customers if row.get("crm_company_id")),
            "active_customers": lifecycle_counts.get("active", 0)
            + lifecycle_counts.get("new", 0),
            "customers_at_risk": lifecycle_counts.get("at_risk", 0)
            + lifecycle_counts.get("dormant", 0),
            "omnichannel_customers": source_counts.get("both", 0),
            "high_value_customers": sum(
                1 for row in customers if row.get("value_tier") in {"A", "B"}
            ),
            "campaign_ready": sum(
                1
                for row in customers
                if row.get("email_ready") or row.get("whatsapp_ready")
            ),
            "customer_product_opportunities": len(customer_product_opportunities),
        },
        "source_counts": dict(source_counts),
        "lifecycle_counts": dict(lifecycle_counts),
        "type_counts": dict(type_counts),
        "region_counts": dict(region_counts),
        "acquisition_by_month": [
            {"month": month, **values} for month, values in sorted(by_month.items())
        ][-18:],
        "segments": segments,
        "opportunity_counts": dict(opportunity_counts),
        "top_opportunities": top_opportunities,
        "customer_product_opportunities": customer_product_opportunities,
        "product_opportunity_diagnostics": product_opportunity_diagnostics,
        "facto_ranking": facto_ranking,
        "tiendanube_ranking": tiendanube_ranking,
        "sales_products": sales_products,
        "product_opportunity_methodology": (
            "Cruce seguro por SKU, nombre normalizado o una unica coincidencia contenida entre "
            "las compras del cliente y el inventario vigente. Los analisis nuevos usan la fecha "
            "real por producto; los historicos anteriores usan la ultima compra general del "
            "cliente y lo declaran expresamente. Informa dias sin venta observada y no estima "
            "antiguedad en bodega cuando Facto no entrega la fecha de ingreso."
        ),
        "methodology": (
            "Unión automática sólo por RUT, email o teléfono exactos. "
            "Facto es la fuente de venta neta; Tiendanube identifica el canal web "
            "sin duplicar ese ingreso. El puntaje comercial combina valor, recencia, "
            "frecuencia, contacto y presencia en ambos canales; siempre requiere revisión humana."
        ),
    }
