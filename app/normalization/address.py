import re

from unidecode import unidecode

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")

_ABBREVIATIONS = {
    "AVDA": "AV",
    "AVENIDA": "AV",
    "PSJE": "PJE",
    "PASAJE": "PJE",
    "DEPTO": "DPTO",
    "DEPARTAMENTO": "DPTO",
}


def normalize_address(raw: str | None) -> str | None:
    """Normalize a street address for dedup matching: accents stripped,
    uppercased, punctuation collapsed, common abbreviation variants
    unified (AVDA/AVENIDA -> AV, etc)."""
    if not raw or not raw.strip():
        return None

    value = unidecode(raw).upper()
    value = _NON_ALNUM_RE.sub(" ", value)
    value = _WHITESPACE_RE.sub(" ", value).strip()

    tokens = [_ABBREVIATIONS.get(tok, tok) for tok in value.split(" ")]
    return " ".join(tokens) or None
