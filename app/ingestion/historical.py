from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
from unidecode import unidecode

from app.ingestion.excel_parser import read_workbook_tables
from app.normalization.phone import normalize_phone
from app.normalization.rut import normalize_rut

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_EMAIL_SPLIT = re.compile(r"[;,\s]+")

ALIASES = {
    "legacy_code": {"CODIGO", "CODIGO CLIENTE", "CUSTOMER CODE"},
    "legal_name": {"RAZON SOCIAL", "EMPRESA", "CLIENTE", "NOMBRE EMPRESA"},
    "rut": {"RUT", "RUT EMPRESA", "TAX ID"},
    "email": {"EMAIL", "E MAIL", "CORREO", "MAIL"},
    "phone": {"TELEFONO 1", "TELEFONO", "FONO", "PHONE"},
}


def _header(value: Any) -> str:
    return _NON_ALNUM.sub(" ", unidecode(str(value)).upper()).strip()


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _mapping(headers: list[str]) -> dict[str, str]:
    normalized = {_header(header): header for header in headers}
    result: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        match = next((normalized[alias] for alias in aliases if alias in normalized), None)
        if match:
            result[field] = match
    return result


def _emails(raw: str) -> tuple[list[str], list[str]]:
    valid: list[str] = []
    invalid: list[str] = []
    for item in _EMAIL_SPLIT.split(raw.lower().strip()):
        if not item:
            continue
        (valid if _EMAIL.fullmatch(item) else invalid).append(item)
    return list(dict.fromkeys(valid)), list(dict.fromkeys(invalid))


def _identity(row: dict[str, Any]) -> str:
    if row["rut_normalized"]:
        return f"rut:{row['rut_normalized']}"
    if row["legacy_code"]:
        return f"code:{row['legacy_code'].casefold()}"
    emails = row["emails"]
    if emails:
        return f"email:{emails[0]}"
    if row["phone_normalized"]:
        return f"phone:{row['phone_normalized']}"
    return f"name:{_header(row['legal_name'])}"


@dataclass(frozen=True)
class HistoricalImportResult:
    filename: str
    sha256: str
    relationship_date: str | None
    sheets: list[str]
    rows: list[dict[str, Any]]
    stats: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "relationship_date": self.relationship_date,
            "sheets": self.sheets,
            "rows": self.rows,
            "preview": self.rows[:25],
            "stats": self.stats,
        }


def normalize_historical_file(
    content: bytes, filename: str, relationship_date: date | None = None
) -> HistoricalImportResult:
    tables = read_workbook_tables(content, filename)
    normalized_rows: list[dict[str, Any]] = []
    errors: Counter[str] = Counter()

    for sheet_name, frame in tables:
        mapping = _mapping([str(column) for column in frame.columns])
        if "legal_name" not in mapping:
            errors["sheets_without_legal_name"] += 1
            continue
        for position, (_, series) in enumerate(frame.iterrows(), start=2):
            raw = {field: _text(series.get(column)) for field, column in mapping.items()}
            if not any(raw.values()):
                continue
            legal_name = raw.get("legal_name", "")
            if not legal_name:
                errors["rows_without_legal_name"] += 1
                continue
            rut_raw = raw.get("rut", "")
            rut = normalize_rut(rut_raw)
            email_values, invalid_emails = _emails(raw.get("email", ""))
            phone_raw = raw.get("phone", "")
            phone = normalize_phone(phone_raw)
            flags: list[str] = ["territorio_desconocido", "contacto_historico_no_verificado"]
            if rut_raw and not rut:
                flags.append("rut_invalido")
            if invalid_emails:
                flags.append("email_invalido")
            if phone_raw and not phone:
                flags.append("telefono_ambiguo")
            row = {
                "legacy_code": raw.get("legacy_code", ""),
                "legal_name": legal_name,
                "rut_raw": rut_raw,
                "rut_normalized": rut or "",
                "rut_valid": bool(rut),
                "emails": email_values,
                "invalid_emails": invalid_emails,
                "phone_raw": phone_raw,
                "phone_normalized": phone or "",
                "relationship_date": relationship_date.isoformat() if relationship_date else None,
                "territory_status": "unknown",
                "verification_status": "historical_unverified",
                "flags": flags,
                "provenance": [{"sheet": sheet_name, "row": position}],
            }
            row["identity_key"] = _identity(row)
            normalized_rows.append(row)

    consolidated: dict[str, dict[str, Any]] = {}
    for row in normalized_rows:
        key = row["identity_key"]
        current = consolidated.get(key)
        if current is None:
            consolidated[key] = row
            continue
        current["provenance"].extend(row["provenance"])
        current["emails"] = list(dict.fromkeys([*current["emails"], *row["emails"]]))
        current["invalid_emails"] = list(
            dict.fromkeys([*current["invalid_emails"], *row["invalid_emails"]])
        )
        current["flags"] = list(dict.fromkeys([*current["flags"], *row["flags"]]))
        for field in ("legacy_code", "rut_raw", "rut_normalized", "phone_raw", "phone_normalized"):
            if not current[field] and row[field]:
                current[field] = row[field]

    rows = list(consolidated.values())
    stats = {
        "source_rows": len(normalized_rows),
        "entities": len(rows),
        "duplicates_consolidated": len(normalized_rows) - len(rows),
        "valid_ruts": sum(bool(row["rut_valid"]) for row in rows),
        "valid_emails": sum(bool(row["emails"]) for row in rows),
        "valid_phones": sum(bool(row["phone_normalized"]) for row in rows),
        "needs_review": sum(bool(set(row["flags"]) - {"territorio_desconocido", "contacto_historico_no_verificado"}) for row in rows),
        **errors,
    }
    return HistoricalImportResult(
        filename=filename,
        sha256=hashlib.sha256(content).hexdigest(),
        relationship_date=relationship_date.isoformat() if relationship_date else None,
        sheets=[name for name, _ in tables],
        rows=rows,
        stats=stats,
    )
