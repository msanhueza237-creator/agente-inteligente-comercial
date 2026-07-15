from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from unidecode import unidecode

from app.normalization.address import normalize_address
from app.normalization.name import normalize_name
from app.normalization.phone import normalize_phone
from app.normalization.rut import normalize_rut
from app.normalization.website import normalize_website
from app.prospecting.contracts import (
    ProspectCandidate,
    ProspectLocation,
    ProspectingRunSnapshot,
    SourceEvidence,
)

_HVAC_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bclimatiz(?:acion|ador|adores|ar)\b",
        r"\brefrigeracion\b",
        r"\baire(?:s)? acondicionado(?:s)?\b",
        r"\bcalefaccion\b",
        r"\bventilacion\b",
        r"\bhvac\b",
        r"\bfrio industrial\b",
        r"\bbomba(?:s)? de calor\b",
        r"\bchiller(?:s)?\b",
        r"\bair conditioning contractor\b",
        r"\bhvac contractor\b",
        r"\bheating contractor\b",
        r"\brefrigeration\b",
    )
)


def normalize_geo(value: str | None) -> str:
    normalized = unidecode(value or "").upper()
    normalized = re.sub(r"[^A-Z0-9 ]", " ", normalized)
    normalized = " ".join(normalized.split())
    normalized = re.sub(r"^REGION(?:\s+DE(?:L)?|\s+DEL|\s+LA)?\s+", "", normalized)
    aliases = {
        "RM": "METROPOLITANA",
        "METROPOLITANA DE SANTIAGO": "METROPOLITANA",
        "SANTIAGO METROPOLITAN": "METROPOLITANA",
        "LIBERTADOR GENERAL BERNARDO O HIGGINS": "OHIGGINS",
        "LIBERTADOR BERNARDO O HIGGINS": "OHIGGINS",
        "O HIGGINS": "OHIGGINS",
        "LA ARAUCANIA": "ARAUCANIA",
        "NUBLE": "NUBLE",
        "AYSEN DEL GENERAL CARLOS IBANEZ DEL CAMPO": "AYSEN",
        "AYSEN DEL GENERAL CARLOS IBANEZ": "AYSEN",
        "MAGALLANES Y DE LA ANTARTICA CHILENA": "MAGALLANES",
        "MAGALLANES Y ANTARTICA CHILENA": "MAGALLANES",
    }
    return aliases.get(normalized, normalized)


def is_hvac_relevant(candidate: ProspectCandidate) -> bool:
    text = " ".join(
        unidecode(value).lower()
        for value in (
            candidate.name,
            candidate.trade_name,
            candidate.category,
            candidate.description,
            *candidate.specialties,
        )
        if value
    )
    text = re.sub(r"[_-]+", " ", text)
    return any(pattern.search(text) for pattern in _HVAC_PATTERNS)


def is_in_requested_territory(
    candidate: ProspectCandidate, snapshot: ProspectingRunSnapshot
) -> bool:
    return bool(candidate.locations) and all(
        location_is_in_requested_territory(location, snapshot) for location in candidate.locations
    )


def location_is_in_requested_territory(
    location: ProspectLocation, snapshot: ProspectingRunSnapshot
) -> bool:
    if not location.region_code or not location.comuna_code:
        return False
    return any(
        location.region_code == territory.region_code
        and location.comuna_code == territory.comuna_code
        for territory in snapshot.campaign.territories
    )


def has_business_contact(candidate: ProspectCandidate) -> bool:
    phone_ok = bool(normalize_phone(candidate.phone))
    email_ok = bool(
        candidate.email and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", candidate.email.strip())
    )
    website_ok = bool(normalize_website(candidate.website))
    return phone_ok or email_ok or website_ok


def normalize_evidence_value(field: str, value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value)
    if field == "rut":
        return normalize_rut(text)
    if field == "phone":
        return normalize_phone(text)
    if field == "website":
        return normalize_website(text)
    if field in {"name", "trade_name"}:
        return normalize_name(text)[0]
    if field == "email":
        return text.strip().casefold()
    if field == "location.address":
        return normalize_address(text)
    if field in {"location.region_name", "location.comuna_name"}:
        return normalize_geo(text)
    if field in {"location.region_code", "location.comuna_code"}:
        return text.strip()
    if field == "description":
        return " ".join(unidecode(text).casefold().split())
    return " ".join(text.casefold().split())


def has_compatible_evidence(
    candidate: ProspectCandidate,
    field: str,
    value: str | None,
    *,
    location_index: int | None = None,
    evidence_items: Iterable[SourceEvidence] | None = None,
) -> bool:
    expected = normalize_evidence_value(field, value)
    if expected is None:
        return False
    accepted_fields = {field}
    if location_index is not None and field.startswith("location."):
        accepted_fields.add(f"locations[{location_index}].{field.removeprefix('location.')}")
    return any(
        evidence.field in accepted_fields
        and normalize_evidence_value(field, evidence.value) == expected
        for evidence in (evidence_items if evidence_items is not None else candidate.evidence)
    )


def sanitize_unsubstantiated_external_fields(
    candidate: ProspectCandidate,
) -> ProspectCandidate:
    """Strip optional source data which cannot be traced to matching evidence.

    Classification and scoring run before this function, so an unsupported
    value can influence the transparent derived score but is never sent to the
    CRM as if it had been verified externally.
    """

    updates: dict[str, str | None] = {}
    for field_name in ("rut", "trade_name", "phone", "email", "website", "description"):
        value = getattr(candidate, field_name)
        if value and not has_compatible_evidence(candidate, field_name, value):
            updates[field_name] = None

    sanitized_locations: list[ProspectLocation] = []
    for location in candidate.locations:
        location_updates: dict[str, str | None] = {}
        for attribute, evidence_field in (
            ("region_name", "location.region_name"),
            ("comuna_name", "location.comuna_name"),
            ("address", "location.address"),
        ):
            value = getattr(location, attribute)
            if value and not has_compatible_evidence(candidate, evidence_field, value):
                location_updates[attribute] = None
        sanitized_locations.append(location.model_copy(update=location_updates))

    # The candidate contract always places its canonical location first.
    updates["locations"] = sanitized_locations
    updates["location"] = sanitized_locations[0]
    return candidate.model_copy(update=updates)


def has_complete_evidence(candidate: ProspectCandidate) -> bool:
    if not has_compatible_evidence(candidate, "name", candidate.name):
        return False

    for field_name in ("rut", "trade_name", "phone", "email", "website", "description"):
        value = getattr(candidate, field_name)
        if value and not has_compatible_evidence(candidate, field_name, value):
            return False

    for location_index, location in enumerate(candidate.locations):
        if not location.region_code or not location.comuna_code:
            return False
        for attribute, evidence_field in (
            ("region_code", "location.region_code"),
            ("region_name", "location.region_name"),
            ("comuna_code", "location.comuna_code"),
            ("comuna_name", "location.comuna_name"),
            ("address", "location.address"),
        ):
            value = getattr(location, attribute)
            if value and not has_compatible_evidence(
                candidate,
                evidence_field,
                value,
                location_index=location_index,
            ):
                return False

    return {"category", "score"}.issubset(candidate.derived_provenance)


@dataclass(frozen=True)
class QualityResult:
    accepted: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def validate_candidate(
    candidate: ProspectCandidate, snapshot: ProspectingRunSnapshot
) -> QualityResult:
    reasons: list[str] = []
    if not is_hvac_relevant(candidate):
        reasons.append("not_hvac_related")
    if not is_in_requested_territory(candidate, snapshot):
        reasons.append("outside_requested_territory")
    if not has_business_contact(candidate):
        reasons.append("missing_business_contact")
    if not has_complete_evidence(candidate):
        reasons.append("missing_required_evidence")
    if (
        candidate.category
        and candidate.category not in snapshot.campaign.target_types
        and not (
            candidate.category == "otro" and "target_type_unconfirmed" in candidate.review_flags
        )
    ):
        reasons.append("outside_target_types")
    return QualityResult(accepted=not reasons, reasons=tuple(reasons))
