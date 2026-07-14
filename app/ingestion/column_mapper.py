"""Suggests a mapping from arbitrary Excel/CSV column headers (client files
are never in a fixed format) to Prospect fields, using fuzzy header
matching. The suggestion is meant to be reviewed/adjusted by a human in the
upload UI, not applied blindly.
"""

import re

from rapidfuzz import fuzz, process
from unidecode import unidecode

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")

MATCH_THRESHOLD = 75

# field -> known Spanish/English header aliases seen in client spreadsheets.
FIELD_ALIASES: dict[str, list[str]] = {
    "name": ["nombre", "empresa", "razon social", "nombre empresa", "company", "name", "cliente"],
    "trade_name": ["nombre fantasia", "fantasia", "trade name", "nombre comercial"],
    "rut": ["rut", "rut empresa", "tax id"],
    "category": ["categoria", "tipo", "tipo de empresa", "rubro", "category"],
    "region": ["region"],
    "comuna": ["comuna"],
    "city": ["ciudad", "city"],
    "address": ["direccion", "domicilio", "address"],
    "phone": ["telefono", "fono", "celular", "contacto telefono", "phone", "telefono contacto"],
    "email": ["email", "correo", "correo electronico", "mail"],
    "website": ["sitio web", "web", "pagina web", "website", "url"],
    "notes": ["notas", "observaciones", "comentarios", "notes"],
}


def _normalize_header(value: str) -> str:
    value = unidecode(str(value)).upper()
    value = _NON_ALNUM_RE.sub(" ", value)
    return _WHITESPACE_RE.sub(" ", value).strip()


_ALIAS_INDEX: list[tuple[str, str]] = [
    (field, _normalize_header(alias)) for field, aliases in FIELD_ALIASES.items() for alias in aliases
]
_ALIAS_CHOICES = [alias for _, alias in _ALIAS_INDEX]


def suggest_column_mapping(headers: list[str]) -> dict[str, str | None]:
    """Return {prospect_field: original_header_or_None} for every known
    field, choosing the best-scoring header for each field without
    reusing a header across two fields.
    """
    normalized_headers = {h: _normalize_header(h) for h in headers}

    # (score, field, header) candidates, best first.
    candidates: list[tuple[float, str, str]] = []
    for header, norm in normalized_headers.items():
        match_result = process.extractOne(norm, _ALIAS_CHOICES, scorer=fuzz.WRatio)
        if match_result is None:
            continue
        alias, score, idx = match_result
        field = _ALIAS_INDEX[idx][0]
        if score >= MATCH_THRESHOLD:
            candidates.append((score, field, header))

    candidates.sort(key=lambda c: c[0], reverse=True)

    mapping: dict[str, str | None] = {field: None for field in FIELD_ALIASES}
    used_headers: set[str] = set()
    for _score, field, header in candidates:
        if mapping[field] is not None or header in used_headers:
            continue
        mapping[field] = header
        used_headers.add(header)

    return mapping
