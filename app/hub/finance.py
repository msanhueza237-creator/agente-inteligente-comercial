from __future__ import annotations

import base64
import binascii
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any

from pypdf import PdfReader

from app.hub.inventory import payload_rows

VAT_FACTOR = Decimal("1.19")
EXEMPT_DOCUMENT_TYPE_IDS = {32, 33, 41, 42}
PURCHASE_CREDIT_NOTE_TYPE_IDS = {28}
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

FACTO_PDF_MAX_BYTES = 15_000_000
FACTO_PENDING_BALANCE_PATTERN = re.compile(
    r"saldo\s+pendiente\s+a\s+pagar\s+al\s+"
    r"(?P<as_of>\d{2}[-/.]\d{2}[-/.]\d{4})\s*"
    r"(?:\(\s*este\s+documento\s*\))?\s*"
    r"\$?\s*(?P<amount>\d[\d.\s]*(?:,\d{1,2})?)",
    flags=re.IGNORECASE,
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


def _document_id(document: dict[str, Any]) -> str:
    header = _nested_dict(document, "header", "encabezado")
    value = _first(document, "document_id", "id") or _first(
        header, "document_id", "id"
    )
    return str(value or "").strip()


def _document_number(document: dict[str, Any]) -> str:
    header = _nested_dict(document, "header", "encabezado")
    value = _first(
        document,
        "document_number",
        "number",
        "folio",
        "reference_number",
    ) or _first(
        header,
        "document_number",
        "number",
        "folio",
        "reference_number",
    )
    return str(value or "").strip()


def _payment_conditions(document: dict[str, Any]) -> str:
    containers = [
        _nested_dict(document, "header", "encabezado"),
        *[
            value
            for key in (
                "payment",
                "payment_info",
                "payment_information",
                "financial",
                "credit",
                "collection",
                "receivable",
            )
            if isinstance((value := document.get(key)), dict)
        ],
        document,
    ]
    for container in containers:
        value = _first(
            container,
            "payment_conditions",
            "payment_condition",
            "payment_terms",
            "payment_term",
            "condiciones_pago",
            "condicion_pago",
            "forma_pago",
        )
        if isinstance(value, dict):
            value = _first(
                value,
                "value",
                "code",
                "days",
                "description",
                "name",
            )
        if isinstance(value, list):
            values = [
                str(
                    _first(item, "value", "code", "days", "description", "name")
                    if isinstance(item, dict)
                    else item
                ).strip()
                for item in value
            ]
            value = ",".join(item for item in values if item)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _explicit_due_date(document: dict[str, Any]) -> date | None:
    containers = [
        _nested_dict(document, "header", "encabezado"),
        *[
            value
            for key in (
                "payment",
                "payment_info",
                "payment_information",
                "financial",
                "credit",
                "collection",
                "receivable",
            )
            if isinstance((value := document.get(key)), dict)
        ],
        document,
    ]
    value = None
    for container in containers:
        value = _first(
            container,
            "due_date",
            "payment_due_date",
            "expiration_date",
            "expiry_date",
            "fecha_vencimiento",
            "fecha_pago",
        )
        if value:
            break
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _document_due_date(document: dict[str, Any]) -> tuple[date | None, str]:
    explicit = _explicit_due_date(document)
    if explicit:
        return explicit, "facto"
    issued = _document_date(document)
    conditions = _payment_conditions(document)
    if not issued or not conditions:
        return None, "sin_fecha"
    days = [int(value) for value in re.findall(r"\d+", conditions)]
    if not days:
        return None, "sin_fecha"
    return issued + timedelta(days=max(days)), "condicion_pago"


def _payment_classification(document: dict[str, Any]) -> str:
    """Classify Facto's declared payment terms without inventing receivables."""

    conditions = _payment_conditions(document)
    normalized = conditions.casefold()
    values = [int(value) for value in re.findall(r"\d+", normalized)]
    if any(value > 0 for value in values):
        return "credit"
    if any(
        marker in normalized
        for marker in ("credito", "crédito", "credit", "cuotas", "plazo")
    ):
        return "credit"
    if values and all(value == 0 for value in values):
        return "cash"
    if any(
        marker in normalized
        for marker in ("contado", "cash", "inmediato", "immediate")
    ):
        return "cash"

    issued = _document_date(document)
    due = _explicit_due_date(document)
    if issued and due:
        return "credit" if due > issued else "cash"
    return "unknown"


def _is_credit_document(document: dict[str, Any]) -> bool:
    return _payment_classification(document) == "credit"


def _payment_document_id(payment: dict[str, Any]) -> str:
    value = _first(payment, "document_id", "invoice_id", "document")
    if isinstance(value, dict):
        value = _first(value, "document_id", "id")
    return str(value or "").strip()


def _payment_date(payment: dict[str, Any]) -> date | None:
    value = _first(payment, "payment_date", "date", "created_at", "fecha_pago")
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _payment_amount(payment: dict[str, Any]) -> Decimal:
    return max(
        Decimal("0"),
        _decimal(_first(payment, "payment_amount", "amount", "monto_pago")) or Decimal("0"),
    )


def _reported_outstanding_amount(document: dict[str, Any]) -> Decimal | None:
    """Read only a balance explicitly reported by Facto's collections source."""

    containers = [
        *[
            value
            for key in (
                "collection",
                "receivable",
                "payment",
                "payment_info",
                "payment_information",
                "financial",
                "totals",
                "amounts",
            )
            if isinstance((value := document.get(key)), dict)
        ],
        document,
    ]
    for container in containers:
        value = _first(
            container,
            "pending_balance",
            "pending_amount",
            "outstanding_balance",
            "outstanding_amount",
            "balance_due",
            "amount_due",
            "receivable_balance",
            "receivable_amount",
            "remaining_balance",
            "remaining_amount",
            "open_balance",
            "open_amount",
            "saldo_pendiente",
            "monto_pendiente",
            "saldo_por_cobrar",
            "monto_por_cobrar",
            "saldo_actual",
            "saldo",
        )
        parsed = _decimal(value)
        if parsed is not None:
            return max(Decimal("0"), parsed)
    return None


def _parse_facto_pending_balance_text(text: str) -> tuple[Decimal, date] | None:
    """Read Facto's exact PDF footer without inferring debt from invoice totals."""

    normalized = re.sub(r"\s+", " ", text or "").strip()
    match = FACTO_PENDING_BALANCE_PATTERN.search(normalized)
    if not match:
        return None
    raw_amount = match.group("amount").replace(" ", "").replace(".", "")
    raw_amount = raw_amount.replace(",", ".")
    try:
        amount = Decimal(raw_amount)
    except (InvalidOperation, ValueError):
        return None
    try:
        day, month, year = (
            int(value)
            for value in re.split(r"[-/.]", match.group("as_of"))
        )
        as_of = date(year, month, day)
    except (TypeError, ValueError):
        return None
    return max(Decimal("0"), amount), as_of


def _facto_document_pdf(document: dict[str, Any]) -> str:
    """Locate only Facto's documented electronic-document PDF field."""

    electronic_document = document.get("electronic_document")
    candidates = [
        electronic_document.get("document_pdf")
        if isinstance(electronic_document, dict)
        else None,
        document.get("document_pdf"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _receivable_from_facto_pdf(document: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a dated outstanding balance from the last pages of a Facto PDF."""

    encoded = _facto_document_pdf(document)
    if not encoded:
        return None
    if encoded.startswith("data:"):
        _, separator, encoded = encoded.partition(",")
        if not separator:
            return None
    compact = re.sub(r"\s+", "", encoded)
    if not compact or len(compact) > FACTO_PDF_MAX_BYTES * 2:
        return None
    compact += "=" * (-len(compact) % 4)
    try:
        pdf_bytes = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not pdf_bytes.startswith(b"%PDF-") or len(pdf_bytes) > FACTO_PDF_MAX_BYTES:
        return None
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=False)
        pages = reader.pages[-2:] if len(reader.pages) > 2 else reader.pages
        text = "\n".join(page.extract_text() or "" for page in pages)
    except Exception:  # noqa: BLE001
        return None
    parsed = _parse_facto_pending_balance_text(text)
    if not parsed:
        return None
    amount, as_of = parsed
    return {
        "document_id": _document_id(document),
        "document_number": _document_number(document),
        "saldo_pendiente": float(amount),
        "saldo_pendiente_fecha": as_of.isoformat(),
        "collection_source": "facto_document_pdf",
    }


def _facto_pdf_receivables(
    documents: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build exact per-document balances and report extraction coverage."""

    rows: list[dict[str, Any]] = []
    pdf_documents = 0
    for document in documents:
        if not isinstance(document, dict) or not _facto_document_pdf(document):
            continue
        pdf_documents += 1
        receivable = _receivable_from_facto_pdf(document)
        if receivable is not None:
            rows.append(receivable)
    matched = len(rows)
    return rows, {
        "documents_examined": len(documents),
        "documents_with_pdf": pdf_documents,
        "documents_with_balance": matched,
        "percent": round(matched / pdf_documents * 100, 1) if pdf_documents else 0,
        "complete": bool(pdf_documents and matched == pdf_documents),
    }


def _merge_receivable_rows(
    receivables: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Complete collection rows with their invoice metadata without altering balances."""

    by_id = {
        document_id: document
        for document in documents
        if (document_id := _document_id(document))
    }
    by_number = {
        document_number: document
        for document in documents
        if (document_number := _document_number(document))
    }
    nested_keys = (
        "header",
        "encabezado",
        "customer",
        "client",
        "receiver",
        "recipient",
        "receptor",
        "collection",
        "receivable",
        "totals",
        "total",
        "amounts",
        "montos",
    )
    merged_rows: list[dict[str, Any]] = []
    for receivable in receivables:
        base = by_id.get(_document_id(receivable)) or by_number.get(
            _document_number(receivable)
        )
        if not base:
            merged_rows.append(receivable)
            continue
        merged = {**base, **receivable}
        for key in nested_keys:
            base_value = base.get(key)
            receivable_value = receivable.get(key)
            if isinstance(base_value, dict) or isinstance(receivable_value, dict):
                merged[key] = {
                    **(base_value if isinstance(base_value, dict) else {}),
                    **(
                        receivable_value
                        if isinstance(receivable_value, dict)
                        else {}
                    ),
                }
        merged_rows.append(merged)
    return merged_rows


def _embedded_payments(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in documents:
        document_id = _document_id(document)
        for key in ("payments", "payment_records", "pagos"):
            value = document.get(key)
            if isinstance(value, dict):
                candidates = payload_rows(value, "data", "payments", "items")
            elif isinstance(value, list):
                candidates = [item for item in value if isinstance(item, dict)]
            else:
                continue
            for candidate in candidates:
                rows.append(
                    candidate
                    if _payment_document_id(candidate)
                    else {**candidate, "document_id": document_id}
                )
    return rows


def _collections_snapshot(
    documents: list[dict[str, Any]],
    *,
    payments_payload: Any | None,
    receivables_payload: Any | None,
    cutoff: date,
) -> dict[str, Any]:
    official_receivable_rows = (
        payload_rows(
            receivables_payload,
            "receivables",
            "unpaid_documents",
            "accounts_receivable",
            "documents",
            "items",
        )
        if receivables_payload is not None
        else []
    )
    official_receivables_available = receivables_payload is not None
    pdf_receivable_rows, pdf_coverage = _facto_pdf_receivables(documents)
    collection_mode = (
        "facto_receivables"
        if official_receivables_available
        else "facto_document_pdf"
        if pdf_receivable_rows
        else "unavailable"
    )
    receivable_rows = (
        official_receivable_rows
        if official_receivables_available
        else pdf_receivable_rows
    )
    receivables_available = official_receivables_available or bool(pdf_receivable_rows)
    external_payments = (
        payload_rows(payments_payload, "data", "payments", "items")
        if payments_payload is not None
        else []
    )
    embedded = _embedded_payments(documents)
    payments_available = payments_payload is not None or bool(embedded)
    # A payment ledger is useful evidence for documentary cash flow, but it is
    # not the same dataset as Facto's Cobranza -> Documentos impagos. It may be
    # partial, omit credit notes or contain applications that cannot be matched
    # safely. A collections row or the exact dated balance printed by Facto in
    # its own document PDF can be used; invoice totals alone never can.
    authoritative_available = receivables_available
    payments = [*external_payments, *embedded]
    payments_by_month: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"amount": Decimal("0"), "payments": 0}
    )
    for payment in payments:
        if not isinstance(payment, dict):
            continue
        amount = _payment_amount(payment)
        paid_on = _payment_date(payment)
        month = paid_on.strftime("%Y-%m") if paid_on else "sin_fecha"
        payments_by_month[month]["amount"] += amount
        payments_by_month[month]["payments"] += 1

    customer_rows: dict[str, dict[str, Any]] = {}
    document_rows: list[dict[str, Any]] = []
    aging: dict[str, dict[str, Decimal | int]] = {
        "Al dia": {"amount": Decimal("0"), "documents": 0},
        "1-30 dias": {"amount": Decimal("0"), "documents": 0},
        "31-60 dias": {"amount": Decimal("0"), "documents": 0},
        "61-90 dias": {"amount": Decimal("0"), "documents": 0},
        "Mas de 90 dias": {"amount": Decimal("0"), "documents": 0},
        "Sin vencimiento": {"amount": Decimal("0"), "documents": 0},
    }
    observed_amount = Decimal("0")
    overdue_amount = Decimal("0")
    due_next_30 = Decimal("0")
    reviewed_documents = 0
    reviewed_amount = Decimal("0")
    classification_documents = {"credit": 0, "cash": 0, "unknown": 0}
    classification_amounts = {
        "credit": Decimal("0"),
        "cash": Decimal("0"),
        "unknown": Decimal("0"),
    }

    source_documents = (
        _merge_receivable_rows(receivable_rows, documents)
        if receivables_available
        else documents
    )
    for document in source_documents:
        if not isinstance(document, dict):
            continue
        _, _, gross = _amounts(document)
        classification = _payment_classification(document)
        reviewed_documents += 1
        reviewed_amount += gross
        classification_documents[classification] += 1
        classification_amounts[classification] += gross
        # A condition of payment does not prove that an invoice remains unpaid.
        # Without Facto's collections balance or a complete payment ledger the
        # CRM must not convert issued invoices into accounts receivable.
        if not receivables_available:
            continue
        document_id = _document_id(document)
        reported_amount = _reported_outstanding_amount(document)
        if reported_amount is None:
            # A collections row without an explicit balance is not safe to
            # interpret as unpaid.
            continue
        amount = reported_amount
        paid = max(Decimal("0"), gross - amount) if gross else Decimal("0")
        if amount <= 0:
            continue
        due_date = _explicit_due_date(document)
        due_source = collection_mode if due_date else "sin_fecha"
        days_overdue = max(0, (cutoff - due_date).days) if due_date else 0
        if due_date is None:
            bucket = "Sin vencimiento"
        elif days_overdue <= 0:
            bucket = "Al dia"
            if 0 <= (due_date - cutoff).days <= 30:
                due_next_30 += amount
        elif days_overdue <= 30:
            bucket = "1-30 dias"
        elif days_overdue <= 60:
            bucket = "31-60 dias"
        elif days_overdue <= 90:
            bucket = "61-90 dias"
        else:
            bucket = "Mas de 90 dias"
        if days_overdue > 0:
            overdue_amount += amount
        observed_amount += amount
        aging[bucket]["amount"] += amount
        aging[bucket]["documents"] += 1
        customer_name, customer_tax_id = _customer(document)
        customer_key = customer_tax_id or customer_name.casefold()
        customer = customer_rows.setdefault(
            customer_key,
            {
                "name": customer_name,
                "tax_id": customer_tax_id,
                "amount": Decimal("0"),
                "overdue": Decimal("0"),
                "due_next_30": Decimal("0"),
                "documents": 0,
                "max_days_overdue": 0,
                "oldest_due_date": None,
            },
        )
        customer["amount"] += amount
        customer["documents"] += 1
        if days_overdue > 0:
            customer["overdue"] += amount
            customer["max_days_overdue"] = max(customer["max_days_overdue"], days_overdue)
        elif due_date and (due_date - cutoff).days <= 30:
            customer["due_next_30"] += amount
        if due_date and (
            customer["oldest_due_date"] is None
            or due_date.isoformat() < customer["oldest_due_date"]
        ):
            customer["oldest_due_date"] = due_date.isoformat()
        document_rows.append(
            {
                "document_id": document_id,
                "document_number": _document_number(document),
                "customer": customer_name,
                "tax_id": customer_tax_id,
                "issue_date": _document_date(document).isoformat()
                if _document_date(document)
                else None,
                "due_date": due_date.isoformat() if due_date else None,
                "due_date_source": due_source,
                "payment_conditions": _payment_conditions(document),
                "gross_amount": float(gross),
                "paid_amount": float(paid),
                "observed_amount": float(amount),
                "balance_as_of": document.get("saldo_pendiente_fecha"),
                "balance_source": document.get("collection_source") or collection_mode,
                "days_overdue": days_overdue,
                "bucket": bucket,
            }
        )

    customers = sorted(
        customer_rows.values(), key=lambda item: item["amount"], reverse=True
    )
    unclassified_documents = classification_documents["unknown"]
    classification_status = (
        "missing"
        if reviewed_documents and unclassified_documents == reviewed_documents
        else "partial"
        if unclassified_documents
        else "complete"
    )
    collection_as_of = cutoff
    if collection_mode == "facto_document_pdf":
        balance_dates: list[date] = []
        for document in source_documents:
            raw_balance_date = (
                document.get("saldo_pendiente_fecha")
                if isinstance(document, dict)
                else None
            )
            if not isinstance(raw_balance_date, str):
                continue
            try:
                balance_dates.append(date.fromisoformat(raw_balance_date[:10]))
            except ValueError:
                continue
        if balance_dates:
            collection_as_of = max(balance_dates)
    return {
        "mode": collection_mode,
        "source": collection_mode,
        "authoritative": authoritative_available,
        "receivables_available": receivables_available,
        "portfolio_complete": (
            True
            if official_receivables_available
            else bool(pdf_coverage["complete"])
        ),
        "pdf_coverage": pdf_coverage,
        "payments_available": payments_available,
        "as_of": collection_as_of.isoformat(),
        "reviewed_documents": reviewed_documents,
        "reviewed_amount": float(reviewed_amount),
        "credit_documents": classification_documents["credit"],
        "credit_amount": float(classification_amounts["credit"]),
        "cash_documents": classification_documents["cash"],
        "cash_amount": float(classification_amounts["cash"]),
        "unclassified_documents": unclassified_documents,
        "unclassified_amount": float(classification_amounts["unknown"]),
        "classification_status": classification_status,
        "observed_amount": float(observed_amount),
        "overdue_amount": float(overdue_amount),
        "due_next_30": float(due_next_30),
        "documents": len(document_rows),
        "overdue_documents": sum(
            1 for item in document_rows if item["days_overdue"] > 0
        ),
        "payments_registered": float(
            sum((_payment_amount(payment) for payment in payments), Decimal("0"))
        ),
        "payment_count": len(payments),
        "aging": [
            {
                "bucket": bucket,
                "amount": float(values["amount"]),
                "documents": values["documents"],
            }
            for bucket, values in aging.items()
            if values["documents"]
        ],
        "customers": [
            {
                **item,
                "amount": float(item["amount"]),
                "overdue": float(item["overdue"]),
                "due_next_30": float(item["due_next_30"]),
            }
            for item in customers
        ],
        "documents_detail": sorted(
            document_rows,
            key=lambda item: (
                -item["days_overdue"],
                -(item["observed_amount"] or 0),
            ),
        ),
        "payments_by_month": [
            {
                "month": month,
                "amount": float(values["amount"]),
                "payments": values["payments"],
            }
            for month, values in sorted(payments_by_month.items())
        ],
        "disclaimer": (
            "Saldo pendiente informado por el recurso oficial de Cobranza de Facto."
            if official_receivables_available
            else (
                "Saldo exacto y fechado leido de la linea 'Saldo pendiente a pagar' "
                "del PDF oficial de cada documento Facto. El total corresponde solo "
                f"a los {pdf_coverage['documents_with_balance']} PDF con esa evidencia; "
                "no se estiman saldos en documentos sin coincidencia."
            )
            if pdf_receivable_rows
            else (
                "Facto no entrego por API el recurso oficial Cobranza -> Documentos "
                "impagos ni un PDF con la linea exacta de saldo pendiente. Los pagos "
                "disponibles son solo informativos y no se usan para calcular deuda. "
                "El CRM no muestra estimaciones de cobranza."
            )
        ),
    }


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
    exempt = _document_type_id(document) in EXEMPT_DOCUMENT_TYPE_IDS
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


def _supplier(document: dict[str, Any]) -> tuple[str, str]:
    """Read the issuer of a received document as the purchase supplier."""

    header = _nested_dict(document, "header", "encabezado")
    party = (
        _nested_dict(document, "issuer", "supplier", "vendor", "provider", "emisor")
        or _nested_dict(header, "issuer", "supplier", "vendor", "provider", "emisor")
    )
    candidates = (party, header, document)

    def find(*keys: str) -> Any:
        for candidate in candidates:
            value = _first(candidate, *keys)
            if value not in (None, ""):
                return value
        return None

    name = str(
        find(
            "issuer_legal_name",
            "issuer_business_name",
            "issuer_name",
            "supplier_legal_name",
            "supplier_business_name",
            "supplier_name",
            "vendor_legal_name",
            "vendor_business_name",
            "vendor_name",
            "provider_legal_name",
            "provider_business_name",
            "provider_name",
            "emisor_razon_social",
            "emisor_nombre",
            "business_name",
            "legal_name",
            "name",
            "razon_social",
        )
        or "Proveedor no identificado"
    ).strip()
    tax_id = str(
        find(
            "issuer_tax_id_code",
            "issuer_tax_id",
            "issuer_rut",
            "supplier_tax_id_code",
            "supplier_tax_id",
            "supplier_rut",
            "vendor_tax_id_code",
            "vendor_tax_id",
            "vendor_rut",
            "provider_tax_id_code",
            "provider_tax_id",
            "provider_rut",
            "emisor_rut",
            "tax_id_code",
            "tax_id",
            "rut",
            "identifier",
        )
        or ""
    ).strip()
    return name, tax_id


def _purchase_net_amount(document: dict[str, Any]) -> Decimal:
    net, _, _ = _amounts(document)
    return -abs(net) if _document_type_id(document) in PURCHASE_CREDIT_NOTE_TYPE_IDS else net


def _annual_comparison(
    dated_amounts: list[tuple[date, Decimal]],
    dated_purchases: list[tuple[date, Decimal]] | None = None,
    *,
    generated_on: date,
) -> dict[str, Any]:
    """Compare current YTD sales and purchases with the same prior-year period."""

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
    monthly_current_purchases = {month: Decimal("0") for month in range(1, 13)}
    monthly_previous_purchases = {month: Decimal("0") for month in range(1, 13)}
    current_ytd = Decimal("0")
    previous_ytd = Decimal("0")
    previous_full_year = Decimal("0")
    current_ytd_purchases = Decimal("0")
    previous_ytd_purchases = Decimal("0")
    previous_full_year_purchases = Decimal("0")
    current_documents = 0
    previous_documents = 0
    current_purchase_documents = 0
    previous_purchase_documents = 0

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

    for issued, net in dated_purchases or []:
        if issued.year == current_year:
            monthly_current_purchases[issued.month] += net
            if issued <= cutoff:
                current_ytd_purchases += net
                current_purchase_documents += 1
        elif issued.year == previous_year:
            monthly_previous_purchases[issued.month] += net
            previous_full_year_purchases += net
            if issued <= previous_cutoff:
                previous_ytd_purchases += net
                previous_purchase_documents += 1

    growth_amount = current_ytd - previous_ytd
    growth_percent = (
        (growth_amount / previous_ytd) * Decimal("100")
        if previous_ytd
        else None
    )
    purchase_growth_amount = current_ytd_purchases - previous_ytd_purchases
    purchase_growth_percent = (
        (purchase_growth_amount / previous_ytd_purchases) * Decimal("100")
        if previous_ytd_purchases
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
        "current_ytd_net_purchases": float(current_ytd_purchases),
        "previous_ytd_net_purchases": float(previous_ytd_purchases),
        "previous_full_year_net_purchases": float(previous_full_year_purchases),
        "purchase_growth_amount": float(purchase_growth_amount),
        "purchase_growth_percent": (
            float(purchase_growth_percent) if purchase_growth_percent is not None else None
        ),
        "current_ytd_purchase_documents": current_purchase_documents,
        "previous_ytd_purchase_documents": previous_purchase_documents,
        "months": [
            {
                "month": month,
                "label": MONTH_LABELS[month - 1],
                "current_net_sales": float(monthly_current[month]),
                "previous_net_sales": float(monthly_previous[month]),
                "current_net_purchases": float(monthly_current_purchases[month]),
                "previous_net_purchases": float(monthly_previous_purchases[month]),
            }
            for month in range(1, 13)
        ],
    }


def extract_financial_snapshot(
    documents_payload: Any,
    product_snapshots: list[dict[str, Any]] | None = None,
    *,
    generated_on: date | None = None,
    purchase_documents_payload: Any | None = None,
    payments_payload: Any | None = None,
    receivables_payload: Any | None = None,
) -> list[dict[str, Any]]:
    """Create one auditable rolling financial summary from issued Facto documents.

    The summary intentionally excludes bank balance, paid/unpaid status and
    expenses until Facto supplies those resources explicitly.
    """

    documents = payload_rows(documents_payload, "data", "documents", "items")
    purchase_documents = payload_rows(
        purchase_documents_payload, "data", "documents", "items"
    )
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
    purchases_by_month: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"net_purchases": Decimal("0"), "documents": 0}
    )
    suppliers: dict[str, dict[str, Any]] = {}
    dated_purchases: list[tuple[date, Decimal]] = []
    net_purchases = Decimal("0")

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

    for document in purchase_documents:
        if not isinstance(document, dict):
            continue
        issued = _document_date(document)
        if issued:
            dates.append(issued)
        net = _purchase_net_amount(document)
        net_purchases += net
        if issued:
            dated_purchases.append((issued, net))
        month = issued.strftime("%Y-%m") if issued else "sin_fecha"
        purchases_by_month[month]["net_purchases"] += net
        purchases_by_month[month]["documents"] += 1
        supplier_name, supplier_tax_id = _supplier(document)
        supplier_key = supplier_tax_id or supplier_name.casefold()
        supplier = suppliers.setdefault(
            supplier_key,
            {
                "name": supplier_name,
                "tax_id": supplier_tax_id,
                "net_purchases": Decimal("0"),
                "documents": 0,
                "years": defaultdict(lambda: {"net_purchases": Decimal("0"), "documents": 0}),
            },
        )
        supplier["net_purchases"] += net
        supplier["documents"] += 1
        if issued:
            supplier["years"][str(issued.year)]["net_purchases"] += net
            supplier["years"][str(issued.year)]["documents"] += 1

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
    top_suppliers = sorted(
        suppliers.values(), key=lambda item: item["net_purchases"], reverse=True
    )
    document_count = len(documents)
    comparison_cutoff = generated_on or (max(dates) if dates else date.today())
    collections = _collections_snapshot(
        documents,
        payments_payload=payments_payload,
        receivables_payload=receivables_payload,
        cutoff=comparison_cutoff,
    )
    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "period_start": min(dates).isoformat() if dates else None,
        "period_end": max(dates).isoformat() if dates else None,
        "document_count": document_count,
        "net_sales": float(net_sales),
        "tax": float(tax),
        "gross_sales": float(gross_sales),
        "net_purchases": float(net_purchases),
        "purchase_document_count": len(purchase_documents),
        "average_net_ticket": float(net_sales / document_count) if document_count else 0,
        "reference_cost_of_sales": float(reference_cost),
        "reference_gross_margin": float(net_sales - reference_cost),
        "reference_margin_available": bool(products and reference_cost),
        "sales_by_month": [
            {"month": month, **{key: float(value) if isinstance(value, Decimal) else value for key, value in values.items()}}
            for month, values in sorted(monthly.items())
        ],
        "purchases_by_month": [
            {
                "month": month,
                **{
                    key: float(value) if isinstance(value, Decimal) else value
                    for key, value in values.items()
                },
            }
            for month, values in sorted(purchases_by_month.items())
        ],
        "year_comparison": _annual_comparison(
            dated_amounts,
            dated_purchases,
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
        "top_suppliers": [
            {
                "name": item["name"],
                "tax_id": item["tax_id"],
                "net_purchases": float(item["net_purchases"]),
                "documents": item["documents"],
                "years": {
                    year: {
                        "net_purchases": float(values["net_purchases"]),
                        "documents": values["documents"],
                    }
                    for year, values in sorted(item["years"].items())
                },
            }
            for item in top_suppliers
        ],
        "supplier_count": len(top_suppliers),
        "purchases_available": bool(purchase_documents),
        "top_products": products[:30],
        "collections": collections,
        "receivables_available": collections["authoritative"],
        "credit_exposure_available": False,
        "expenses_available": bool(purchase_documents),
        "cash_balance_available": False,
        "documentary_cash_flow": {
            "net_sales": float(net_sales),
            "net_purchases": float(net_purchases),
            "documentary_difference": float(net_sales - net_purchases),
            "payments_registered": collections["payments_registered"],
            "payment_count": collections["payment_count"],
            "cash_balance_available": False,
            "bank_balance_available": False,
            "disclaimer": (
                "Ventas menos compras es flujo documental, no saldo disponible. "
                "Caja fisica y bancos requieren sus movimientos y saldos."
            ),
        },
        "source": "facto_read_only",
    }
    return [{"external_id": "rolling-sales-365", "payload": snapshot}]
