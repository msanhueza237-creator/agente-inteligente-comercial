from __future__ import annotations

import argparse
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pdfplumber


SKU_PATTERNS = (
    r"\bST-[A-Z0-9-]+\b",
    r"\bSTB[A-Z0-9-]+\b",
    r"\bBTG-[A-Z0-9-]+\b",
    r"\bACB-[A-Z0-9-]+\b",
    r"\bABC-[A-Z0-9-]+\b",
)


def _number(value: Any) -> float:
    text = str(value or "0").replace("$", "").replace(",", "").strip()
    try:
        return float(Decimal(text))
    except Exception:  # noqa: BLE001
        return 0.0


def _sku(name: str) -> str | None:
    compact = " ".join(name.replace("\n", " ").split()).upper()
    compact = re.sub(r"\b(ST|BTG|ACB|ABC)-\s+", r"\1-", compact)
    for pattern in SKU_PATTERNS:
        match = re.search(pattern, compact)
        if match:
            return match.group(0)
    return None


def extract(
    source: Path,
    *,
    production_start: date,
    as_of: date,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    document_totals: dict[str, float] | None = None
    with pdfplumber.open(source) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables():
                for row in table:
                    first = str(row[0] or "").strip()
                    if first.upper().startswith("TOTAL"):
                        document_totals = {
                            "fob_usd": _number(row[5]),
                            "cartons": _number(row[6]),
                            "gross_weight_kg": _number(row[7]),
                            "total_cbm": _number(row[8]),
                        }
                        continue
                    if not first.isdigit() or len(row) < 9:
                        continue
                    name = " ".join(str(row[1] or "").replace("\n", " ").split())
                    quantity = _number(row[2])
                    total_cbm = _number(row[8])
                    items.append(
                        {
                            "line_number": int(first),
                            "name": name,
                            "sku": _sku(name),
                            "quantity": int(quantity) if quantity.is_integer() else quantity,
                            "unit": str(row[3] or "").strip().lower(),
                            "unit_fob_usd": _number(row[4]),
                            "total_fob_usd": _number(row[5]),
                            "cartons": _number(row[6]),
                            "gross_weight_kg": _number(row[7]),
                            "total_cbm": total_cbm,
                            "unit_cbm": round(total_cbm / quantity, 8) if total_cbm > 0 and quantity > 0 else None,
                            "volume_evidence": "proforma_direct" if total_cbm > 0 else "pending",
                            "status": "in_production",
                            "source_page": page_number,
                        }
                    )
        page_count = len(pdf.pages)

    items.sort(key=lambda item: int(item["line_number"]))
    if [item["line_number"] for item in items] != list(range(1, 150)):
        raise ValueError("La proforma debe contener exactamente las partidas correlativas 1 a 149.")
    if not document_totals:
        raise ValueError("No se encontro la fila TOTAL de la proforma.")

    production_end = production_start + timedelta(days=45)
    port_arrival = production_end + timedelta(days=45)
    warehouse_arrival = port_arrival + timedelta(days=5)
    elapsed = max(0, min(45, (as_of - production_start).days))
    remaining = max(0, (warehouse_arrival - as_of).days)

    return {
        "schema_version": 1,
        "generated_at": as_of.isoformat(),
        "imports": [
            {
                "order_number": "26TDC12",
                "reference": "01/26",
                "supplier": "Chinafore Corporation",
                "proforma_date": "2026-03-03",
                "production_start_date": production_start.isoformat(),
                "production_start_basis": (
                    "Confirmacion del usuario: 3 dias transcurridos de produccion al 2026-07-31."
                ),
                "status": "in_production",
                "inventory_status": "confirmed_inbound",
                "stock_policy": "not_available_until_warehouse_receipt",
                "container": "1x20GP",
                "incoterm": "FOB Ningbo",
                "payment_terms": (
                    "10% T/T anticipo, 20% despues del arribo acordado y saldo 70% "
                    "a 90 dias contra fecha B/L."
                ),
                "timeline": {
                    "production_days": 45,
                    "sea_travel_days": 45,
                    "customs_days": 5,
                    "production_end_date": production_end.isoformat(),
                    "estimated_port_arrival_date": port_arrival.isoformat(),
                    "estimated_warehouse_date": warehouse_arrival.isoformat(),
                    "elapsed_production_days": elapsed,
                    "remaining_total_days": remaining,
                    "production_progress_percent": round(elapsed / 45 * 100, 1),
                },
                "totals": document_totals,
                "items": items,
                "source": {
                    "file": source.name,
                    "pages": page_count,
                    "kind": "proforma_invoice",
                },
            }
        ],
        "sources": [
            {
                "file": source.name,
                "kind": "proforma_invoice",
                "purpose": "Mercaderia confirmada en produccion y trazabilidad de orden 26TDC12.",
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--production-start", type=date.fromisoformat, default=date(2026, 7, 28))
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 7, 31))
    args = parser.parse_args()
    payload = extract(args.source, production_start=args.production_start, as_of=args.as_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
