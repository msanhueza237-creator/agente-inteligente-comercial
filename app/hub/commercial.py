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
        "_product_units": {},
        "_product_families": {},
        "first_purchase_at": None,
        "last_purchase_at": None,
        "source_ids": {},
    }


def _merge_contact(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ("name", "legal_name", "tax_id", "email", "phone", "region", "city"):
        if not target.get(key) and source.get(key):
            target[key] = source[key]
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
        "region": _text(find("region", "state", "receiver_region")),
        "city": _text(find("city", "commune", "comuna", "receiver_city")),
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
        "region": _text(_first(billing, "province", "state", "region")),
        "city": _text(_first(billing, "city", "locality", "commune")),
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
    )


def _product_family(value: Any) -> str:
    name = _normalized_text(value)
    families = (
        (
            "Bombas de condensado",
            ("bomba condensado", "bomba de condensado", "condensate pump"),
        ),
        (
            "Herramientas HVAC",
            (
                "bomba vacio",
                "bomba de vacio",
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


def _record_product_activity(target: dict[str, Any], payload: dict[str, Any]) -> None:
    product_units = target.setdefault("_product_units", {})
    family_units = target.setdefault("_product_families", {})
    for line in _line_rows(payload):
        product_name = _text(
            _find(
                line,
                (
                    "name",
                    "product_name",
                    "description",
                    "descripcion",
                    "detalle",
                    "title",
                ),
                "product",
                "item",
            )
        )
        if not product_name:
            continue
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
        product_units[product_name] = float(
            Decimal(str(product_units.get(product_name, 0))) + quantity
        )
        family = _product_family(product_name)
        family_units[family] = float(
            Decimal(str(family_units.get(family, 0))) + quantity
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
        _record_purchase(
            target,
            _document_date(document),
            source="facto",
            amount=net,
        )
        _record_product_activity(target, document)

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
        _record_purchase(
            target,
            _date(_first(order, "completed_at", "paid_at", "created_at", "date")),
            source="tiendanube",
            amount=_decimal(_first(order, "total", "total_amount", "subtotal")),
        )
        _record_product_activity(target, order)

    rows: list[dict[str, Any]] = []
    for customer in customers.values():
        product_units = customer.pop("_product_units", {})
        family_units = customer.pop("_product_families", {})
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


def build_commercial_report(
    commercial_snapshot: list[dict[str, Any]],
    crm_companies: list[dict[str, Any]],
    financial_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enrich the provider portfolio with reviewed CRM classifications."""

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
        target["region"] = _text(company.get("region")) or target.get("region", "")
        target["city"] = _text(company.get("city")) or target.get("city", "")
        target["source"] = _text(company.get("source"))
        if "crm" not in target["sources"]:
            target["sources"].append("crm")

    for row in customers:
        row.pop("_product_units", None)
        row.pop("_product_families", None)
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
    priority_weights = {"urgent": 4, "high": 3, "medium": 2, "normal": 1}
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
        "facto_ranking": facto_ranking,
        "tiendanube_ranking": tiendanube_ranking,
        "sales_products": sales_products,
        "methodology": (
            "Unión automática sólo por RUT, email o teléfono exactos. "
            "Facto es la fuente de venta neta; Tiendanube identifica el canal web "
            "sin duplicar ese ingreso. El puntaje comercial combina valor, recencia, "
            "frecuencia, contacto y presencia en ambos canales; siempre requiere revisión humana."
        ),
    }
