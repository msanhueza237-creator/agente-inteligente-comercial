"""Deterministic dedup matching. Pure functions operating on plain dicts so
they're cheap to unit test independently of the database.

A "candidate" is the normalized field dict for a prospect being ingested
(new, not yet in the DB). An "existing" is the same shape, taken from a
row already in the `prospects` table.

Field keys expected on both sides: google_place_id, rut, website, phone,
name (normalized), address_normalized, comuna.
"""

from dataclasses import dataclass
from typing import Any, Literal

from rapidfuzz import fuzz

from app.config import get_settings

MatchDecision = Literal["auto_merge", "needs_review", "new"]

# Priority order for the exact-key check.
_EXACT_KEYS = ("rut", "google_place_id", "website", "phone")


@dataclass
class MatchResult:
    decision: MatchDecision
    matched_id: Any | None
    score: float
    reasons: dict


def find_exact_match(candidate: dict, existing: list[dict]) -> tuple[dict, str] | None:
    """Return (matched_row, matched_key) for the first exact key that
    matches, checked in priority order. None if no exact match."""
    for key in _EXACT_KEYS:
        value = candidate.get(key)
        if not value:
            continue
        for row in existing:
            if row.get(key) == value:
                return row, key
    candidate_name = candidate.get("name")
    candidate_comuna = candidate.get("comuna")
    if candidate_name and candidate_comuna:
        for row in existing:
            if row.get("name") == candidate_name and row.get("comuna") == candidate_comuna:
                return row, "name_comuna"
    return None


def fuzzy_score(candidate: dict, existing: dict) -> float:
    """Composite fuzzy score (0-100): 60% name similarity, 40% address+comuna
    similarity."""
    name_score = fuzz.token_sort_ratio(candidate.get("name") or "", existing.get("name") or "")

    candidate_addr = f"{candidate.get('address_normalized') or ''} {candidate.get('comuna') or ''}"
    existing_addr = f"{existing.get('address_normalized') or ''} {existing.get('comuna') or ''}"
    addr_score = fuzz.token_set_ratio(candidate_addr.strip(), existing_addr.strip())

    return name_score * 0.6 + addr_score * 0.4


def find_best_fuzzy_match(candidate: dict, existing: list[dict]) -> tuple[dict, float] | None:
    best_row = None
    best_score = -1.0
    for row in existing:
        score = fuzzy_score(candidate, row)
        if score > best_score:
            best_score = score
            best_row = row
    if best_row is None:
        return None
    return best_row, best_score


def match(candidate: dict, existing: list[dict]) -> MatchResult:
    """Run the full dedup decision for one candidate against a pool of
    existing prospects (typically pre-filtered by region/category for
    performance)."""
    settings = get_settings()

    exact = find_exact_match(candidate, existing)
    if exact is not None:
        row, key = exact
        return MatchResult(
            decision="auto_merge",
            matched_id=row.get("id"),
            score=100.0,
            reasons={"type": "exact", "key": key},
        )

    fuzzy = find_best_fuzzy_match(candidate, existing)
    if fuzzy is not None:
        row, score = fuzzy
        if score >= settings.dedup_fuzzy_review_threshold:
            return MatchResult(
                decision="needs_review",
                matched_id=row.get("id"),
                score=score,
                reasons={"type": "fuzzy", "name_and_address_score": score},
            )

    return MatchResult(decision="new", matched_id=None, score=0.0, reasons={})
