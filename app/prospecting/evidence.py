from __future__ import annotations

import re
from collections.abc import Iterable

from app.normalization.address import normalize_address
from app.normalization.name import normalize_name
from app.normalization.phone import normalize_phone
from app.normalization.rut import normalize_rut
from app.normalization.website import normalize_website
from app.prospecting.contracts import SourceEvidence


_INDEXED_LOCATION_FIELD = re.compile(r"^locations\[\d+]\.(.+)$")


def _normalized_value(field: str, value: str) -> str:
    indexed = _INDEXED_LOCATION_FIELD.fullmatch(field)
    if indexed:
        field = f"location.{indexed.group(1)}"
    if field == "rut":
        return normalize_rut(value) or value.strip().casefold()
    if field == "phone":
        return normalize_phone(value) or value.strip().casefold()
    if field == "website":
        return normalize_website(value) or value.strip().casefold()
    if field in {"name", "trade_name"}:
        return normalize_name(value)[0] or value.strip().casefold()
    if field == "location.address":
        return normalize_address(value) or value.strip().casefold()
    return " ".join(value.casefold().split())


def compact_evidence(
    evidence: Iterable[SourceEvidence], *, max_items: int = 100
) -> list[SourceEvidence]:
    """Collapse repeated observations before the transactional CRM batch."""

    best: dict[tuple[str, str, str, str], SourceEvidence] = {}
    for item in evidence:
        source_identity = item.provider_record_id or item.source_url or ""
        key = (
            item.provider.value,
            source_identity,
            item.field,
            _normalized_value(item.field, item.value),
        )
        previous = best.get(key)
        if previous is None or (item.confidence, item.observed_at) > (
            previous.confidence,
            previous.observed_at,
        ):
            best[key] = item

    def priority(item: SourceEvidence) -> tuple:
        field_priority = {
            "name": 0,
            "rut": 1,
            "phone": 2,
            "email": 2,
            "website": 2,
            "location.region_code": 3,
            "location.comuna_code": 3,
            "location.address": 4,
        }
        indexed = _INDEXED_LOCATION_FIELD.fullmatch(item.field)
        base_field = f"location.{indexed.group(1)}" if indexed else item.field
        return (
            field_priority.get(base_field, 5),
            item.provider.value,
            item.field,
            -(item.confidence),
            -item.observed_at.timestamp(),
        )

    return sorted(best.values(), key=priority)[:max_items]
