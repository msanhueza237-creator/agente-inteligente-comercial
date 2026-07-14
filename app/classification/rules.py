"""Lightweight keyword-based category classification. This is the
deterministic first pass described in the plan (section: Clasificacion y
scoring) -- it covers the unambiguous cases cheaply. Ambiguous/unmatched
text is left for the Claude-based classifier added in Phase 2+
(app/classification/llm_classifier.py, not yet implemented).
"""

import re

from unidecode import unidecode

from app.db.models import ProspectCategory

_KEYWORDS: list[tuple[str, ProspectCategory]] = [
    (r"\bdistribuidor(a)?\b", ProspectCategory.distributor),
    (r"\bimportador(a)?\b", ProspectCategory.distributor),
    (r"\bmayorista\b", ProspectCategory.distributor),
    (r"\binstalador(a)?\s+independiente\b", ProspectCategory.installer_independent),
    (r"\binstalaciones?\b", ProspectCategory.installer_independent),
    (r"\binstalador(a)?\b", ProspectCategory.installer_independent),
    (r"\bclimatizacion\b", ProspectCategory.installer_independent),
    (r"\bmantencion(es)?\b", ProspectCategory.maintenance),
    (r"\bservicio tecnico\b", ProspectCategory.maintenance),
    (r"\brepuestos?\b", ProspectCategory.maintenance),
    (r"\brefrigeracion\b", ProspectCategory.refrigeration),
    (r"\bfrio industrial\b", ProspectCategory.refrigeration),
    (r"\btienda\b", ProspectCategory.retailer),
    (r"\bventa(s)? al detalle\b", ProspectCategory.retailer),
    (r"\bretail\b", ProspectCategory.retailer),
]


def classify_category_from_text(*texts: str | None) -> ProspectCategory | None:
    """Best-effort category guess from free-text (e.g. an uncontrolled
    "rubro"/category column from a client spreadsheet, or scraped copy).
    Returns None when nothing matches -- callers should leave category
    unset rather than guess wrong.
    """
    combined = " ".join(unidecode(t).lower() for t in texts if t)
    if not combined:
        return None

    for pattern, category in _KEYWORDS:
        if re.search(pattern, combined):
            return category

    return None
