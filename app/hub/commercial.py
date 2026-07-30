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
        "tiendanube_gross_sales": 0.0,
        "tiendanube_orders": 0,
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


def _record_purchase(target: dict[str, Any], purchase_date: date | None) -> None:
    if purchase_date is None:
        return
    first = _date(target.get("first_purchase_at"))
    last = _date(target.get("last_purchase_at"))
    target["first_purchase_at"] = min(first, purchase_date).isoformat() if first else purchase_date.isoformat()
    target["last_purchase_at"] = max(last, purchase_date).isoformat() if last else purchase_date.isoformat()


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
        _record_purchase(target, _document_date(document))

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
        )

    rows: list[dict[str, Any]] = []
    for customer in customers.values():
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
                "count": len(candidates),
                "customer_keys": [row.get("customer_key") for row in candidates[:500]],
                "company_ids": [
                    row.get("crm_company_id")
                    for row in candidates
                    if row.get("crm_company_id")
                ][:500],
            }
        )

    segment(
        "web_customers_to_develop",
        "Compradores web para desarrollar",
        "Compraron en Climactiva.cl y aun no tienen venta Facto vinculada.",
        lambda row: row.get("source_channel") == "tiendanube_only",
    )
    segment(
        "dormant_customers",
        "Clientes inactivos para reactivacion",
        "Tienen historial real, contacto disponible y mas de 180 dias sin compra.",
        lambda row: row.get("lifecycle") == "dormant",
    )
    segment(
        "at_risk_customers",
        "Clientes en riesgo",
        "Llevan entre 91 y 180 dias sin compra y requieren revision comercial.",
        lambda row: row.get("lifecycle") == "at_risk",
    )
    segment(
        "hvac_technicians",
        "Tecnicos e instaladores",
        "Clasificacion HVAC revisada en el CRM para campanas tecnicas.",
        lambda row: row.get("crm_type") in {"tecnico", "instalador grande"},
        channel="whatsapp",
    )

    by_month: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {"new_customers": 0, "returning_customers": 0}
    )
    for row in customers:
        first = _date(row.get("first_purchase_at"))
        if first:
            by_month[first.strftime("%Y-%m")]["new_customers"] += 1
        last = _date(row.get("last_purchase_at"))
        if last and first and last != first:
            by_month[last.strftime("%Y-%m")]["returning_customers"] += 1

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
            "facto_net_sales": float(total_net_sales),
            "facto_customers": sum(
                1 for row in customers if "facto" in row.get("sources", [])
            ),
            "tiendanube_customers": sum(
                1 for row in customers if "tiendanube" in row.get("sources", [])
            ),
            "crm_companies": sum(1 for row in customers if row.get("crm_company_id")),
        },
        "source_counts": dict(source_counts),
        "lifecycle_counts": dict(lifecycle_counts),
        "type_counts": dict(type_counts),
        "region_counts": dict(region_counts),
        "acquisition_by_month": [
            {"month": month, **values} for month, values in sorted(by_month.items())
        ][-18:],
        "segments": segments,
        "methodology": (
            "Union automatica solo por RUT, email o telefono exactos. "
            "Facto es la fuente de venta neta; Tiendanube identifica el canal web "
            "sin duplicar ese ingreso."
        ),
    }
