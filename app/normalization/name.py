import re

from unidecode import unidecode

# Ordered longest-first so multi-word forms match before their substrings.
# Each pattern maps to a short canonical code (fits the legal_form(20) column).
_LEGAL_FORMS: list[tuple[str, str]] = [
    ("SOCIEDAD POR ACCIONES", "SPA"),
    ("SOCIEDAD ANONIMA", "SA"),
    ("SOCIEDAD DE RESPONSABILIDAD LIMITADA", "LTDA"),
    ("EMPRESA INDIVIDUAL DE RESPONSABILIDAD LIMITADA", "EIRL"),
    ("LIMITADA", "LTDA"),
    ("SPA", "SPA"),
    ("LTDA", "LTDA"),
    ("EIRL", "EIRL"),
    ("E I R L", "EIRL"),
    ("SA", "SA"),
    ("S A", "SA"),
]

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")


def _strip_punctuation(value: str) -> str:
    return _NON_ALNUM_RE.sub(" ", value)


def normalize_name(raw: str | None) -> tuple[str | None, str | None]:
    """Normalize a company name for dedup matching.

    Returns (normalized_name, legal_form) where legal_form is the detected
    Chilean legal-form suffix (SPA, LTDA, S.A., EIRL, ...) if present, and
    normalized_name has that suffix stripped, accents removed, punctuation
    collapsed, uppercased.
    """
    if not raw or not raw.strip():
        return None, None

    value = unidecode(raw).upper()
    value = _strip_punctuation(value)
    value = _WHITESPACE_RE.sub(" ", value).strip()

    legal_form = None
    for form, code in _LEGAL_FORMS:
        pattern = rf"(^|\s){re.escape(form)}($|\s)"
        if re.search(pattern, value):
            legal_form = code
            value = re.sub(pattern, " ", value).strip()
            break

    value = _WHITESPACE_RE.sub(" ", value).strip()
    return (value or None), legal_form
