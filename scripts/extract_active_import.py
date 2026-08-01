from __future__ import annotations

import argparse
import hashlib
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
    source_name: str | None = None,
    source_message_id: str | None = None,
    source_received_at: date | None = None,
    source_revision: str = "final",
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    document_totals: dict[str, float] | None = None
    with pdfplumber.open(source) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables():
                for row in table:
                    if len(row) < 9:
                        continue
                    first = str(row[0] or "").strip()
                    if first.upper().startswith("TOTAL"):
                        document_totals = {
                            "fob_usd": _number(row[5]),
                            "cartons": _number(row[6]),
                            "gross_weight_kg": _number(row[7]),
                            "total_cbm": _number(row[8]),
                        }
                        continue
                    name = " ".join(str(row[1] or "").replace("\n", " ").split())
                    quantity = _number(row[2])
                    total_fob_usd = _number(row[5])
                    source_line_number = int(first) if first.isdigit() else None
                    # La proforma final contiene una partida real sin numero impreso.
                    # Se conserva como fila trazable en vez de descartarla silenciosamente.
                    if source_line_number is None and not (
                        not first and name and quantity > 0 and total_fob_usd > 0
                    ):
                        continue
                    total_cbm = _number(row[8])
                    items.append(
                        {
                            "line_number": len(items) + 1,
                            "source_line_number": source_line_number,
                            "source_line_label": str(source_line_number) if source_line_number is not None else "s/n",
                            "name": name,
                            "sku": _sku(name),
                            "quantity": int(quantity) if quantity.is_integer() else quantity,
                            "unit": str(row[3] or "").strip().lower(),
                            "unit_fob_usd": _number(row[4]),
                            "total_fob_usd": total_fob_usd,
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

    if not document_totals:
        raise ValueError("No se encontro la fila TOTAL de la proforma.")
    numbered_lines = [
        int(item["source_line_number"])
        for item in items
        if item["source_line_number"] is not None
    ]
    if numbered_lines != list(range(1, 89)):
        raise ValueError("La proforma final debe contener las partidas numeradas correlativas 1 a 88.")
    if len(items) != 89 or sum(item["source_line_number"] is None for item in items) != 1:
        raise ValueError("La proforma final debe contener 89 partidas reales: 88 numeradas y una sin numero impreso.")

    computed_totals = {
        "fob_usd": round(sum(_number(item["total_fob_usd"]) for item in items), 2),
        "cartons": round(sum(_number(item["cartons"]) for item in items), 2),
        "gross_weight_kg": round(sum(_number(item["gross_weight_kg"]) for item in items), 2),
        "total_cbm": round(sum(_number(item["total_cbm"]) for item in items), 2),
    }
    reconciliation_tolerance = {
        "fob_usd": 0.01,
        "cartons": 0.01,
        "gross_weight_kg": 0.01,
        # La suma de los m3 redondeados por partida es 26,83; la proforma
        # imprime 26,84 como total general. Se conserva el total documental.
        "total_cbm": 0.02,
    }
    variances = {
        key: round(document_totals[key] - computed_totals[key], 2)
        for key in document_totals
    }
    mismatches = [
        key
        for key, expected in document_totals.items()
        if abs(computed_totals[key] - expected) > reconciliation_tolerance[key]
    ]
    if mismatches:
        raise ValueError(
            "Los totales calculados no concuerdan con la proforma final: "
            f"{', '.join(mismatches)}. Calculado={computed_totals}; documento={document_totals}."
        )

    production_end = production_start + timedelta(days=45)
    port_arrival = production_end + timedelta(days=45)
    warehouse_arrival = port_arrival + timedelta(days=5)
    elapsed = max(0, min(45, (as_of - production_start).days))
    remaining = max(0, (warehouse_arrival - as_of).days)

    return {
        "schema_version": 2,
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
                    "10% T/T anticipo, 20% despues de la llegada a Chile y saldo 70% "
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
                "reconciliation": {
                    "actual_item_rows": len(items),
                    "numbered_item_rows": len(numbered_lines),
                    "unnumbered_item_rows": len(items) - len(numbered_lines),
                    "computed_totals": computed_totals,
                    "document_totals": document_totals,
                    "matches_document_total": True,
                    "exact_match": all(abs(value) < 0.001 for value in variances.values()),
                    "variances": variances,
                    "warning": (
                        "La fuente contiene una partida LI-BATTERY TUBE BENDER sin numero impreso; "
                        "se conserva como producto real y forma parte del total FOB. La suma de m3 "
                        "redondeados por partida es 26,83 y el total documental impreso es 26,84 m3; "
                        "se utiliza el total documental."
                    ),
                },
                "items": items,
                "source": {
                    "file": source_name or source.name,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "pages": page_count,
                    "kind": "proforma_invoice",
                    "gmail_message_id": source_message_id,
                    "received_at": source_received_at.isoformat() if source_received_at else None,
                    "revision": source_revision,
                },
            }
        ],
        "sources": [
            {
                "file": source_name or source.name,
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
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 8, 1))
    parser.add_argument("--source-name")
    parser.add_argument("--source-message-id")
    parser.add_argument("--source-received-at", type=date.fromisoformat)
    parser.add_argument("--source-revision", default="final")
    args = parser.parse_args()
    payload = extract(
        args.source,
        production_start=args.production_start,
        as_of=args.as_of,
        source_name=args.source_name,
        source_message_id=args.source_message_id,
        source_received_at=args.source_received_at,
        source_revision=args.source_revision,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
