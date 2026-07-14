from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.normalization.address import normalize_address
from app.normalization.name import normalize_name
from app.normalization.phone import normalize_phone
from app.normalization.rut import normalize_rut
from app.normalization.website import normalize_website
from app.prospecting.contracts import DedupDisposition, ProspectCandidate
from app.prospecting.evidence import compact_evidence
from app.prospecting.validation import normalize_geo


_EXACT_PRIORITY = {
    "rut": 0,
    "provider_id": 1,
    "domain": 2,
    "phone": 3,
    "name_comuna_address": 4,
    "name_comuna": 4,
}
_INDEXED_LOCATION_FIELD = re.compile(r"^locations\[(\d+)]\.(.+)$")


@dataclass(frozen=True)
class CandidateMatch:
    disposition: DedupDisposition
    matched_id: str | None = None
    key: str | None = None
    score: float = 0.0


def _provider_match(candidate: ProspectCandidate, existing: ProspectCandidate) -> bool:
    return any(
        provider in existing.provider_ids and existing.provider_ids[provider] == value
        for provider, value in candidate.provider_ids.items()
        if value
    )


def _provider_conflict(candidate: ProspectCandidate, existing: ProspectCandidate) -> bool:
    shared_providers = set(candidate.provider_ids) & set(existing.provider_ids)
    return any(
        candidate.provider_ids[provider].strip()
        != existing.provider_ids[provider].strip()
        for provider in shared_providers
        if candidate.provider_ids[provider].strip()
        and existing.provider_ids[provider].strip()
    )


def _blocking_higher_priority_conflict(
    candidate: ProspectCandidate,
    existing: ProspectCandidate,
    match_key: str,
) -> str | None:
    """Return a contradictory identifier that outranks an exact match.

    Missing identifiers do not contradict each other. A shared provider name
    with two different persistent IDs is a contradiction at provider level;
    one matching provider must not conceal another conflicting one.
    """
    match_priority = _EXACT_PRIORITY[match_key]
    candidate_rut = normalize_rut(candidate.rut)
    existing_rut = normalize_rut(existing.rut)
    if (
        match_priority > _EXACT_PRIORITY["rut"]
        and candidate_rut
        and existing_rut
        and candidate_rut != existing_rut
    ):
        return "rut"
    if match_priority >= _EXACT_PRIORITY["provider_id"] and _provider_conflict(
        candidate, existing
    ):
        return "provider_id"

    candidate_domain = normalize_website(candidate.website)
    existing_domain = normalize_website(existing.website)
    if (
        match_priority > _EXACT_PRIORITY["domain"]
        and candidate_domain
        and existing_domain
        and candidate_domain != existing_domain
    ):
        return "domain"

    candidate_phone = normalize_phone(candidate.phone)
    existing_phone = normalize_phone(existing.phone)
    if (
        match_priority > _EXACT_PRIORITY["phone"]
        and candidate_phone
        and existing_phone
        and candidate_phone != existing_phone
    ):
        return "phone"
    return None


def _exact_key(candidate: ProspectCandidate, existing: ProspectCandidate) -> str | None:
    candidate_rut = normalize_rut(candidate.rut)
    if candidate_rut and candidate_rut == normalize_rut(existing.rut):
        return "rut"
    if _provider_match(candidate, existing):
        return "provider_id"
    candidate_domain = normalize_website(candidate.website)
    if candidate_domain and candidate_domain == normalize_website(existing.website):
        return "domain"
    candidate_phone = normalize_phone(candidate.phone)
    if candidate_phone and candidate_phone == normalize_phone(existing.phone):
        return "phone"
    candidate_name, _ = normalize_name(candidate.name)
    existing_name, _ = normalize_name(existing.name)
    candidate_comuna = normalize_geo(
        candidate.location.comuna_code or candidate.location.comuna_name
    )
    existing_comuna = normalize_geo(existing.location.comuna_code or existing.location.comuna_name)
    if (
        candidate_name
        and candidate_name == existing_name
        and candidate_comuna
        and candidate_comuna == existing_comuna
    ):
        candidate_address = normalize_address(candidate.location.address)
        existing_address = normalize_address(existing.location.address)
        if candidate_address is None and existing_address is None:
            return "name_comuna"
        if (
            candidate_address is not None
            and existing_address is not None
            and candidate_address == existing_address
        ):
            return "name_comuna_address"
    return None


def _fuzzy_score(candidate: ProspectCandidate, existing: ProspectCandidate) -> float:
    candidate_name, _ = normalize_name(candidate.name)
    existing_name, _ = normalize_name(existing.name)
    if not candidate_name or not existing_name:
        return 0.0
    candidate_comuna = normalize_geo(
        candidate.location.comuna_code or candidate.location.comuna_name
    )
    existing_comuna = normalize_geo(existing.location.comuna_code or existing.location.comuna_name)
    if not candidate_comuna or candidate_comuna != existing_comuna:
        return 0.0
    return float(fuzz.token_sort_ratio(candidate_name, existing_name))


def match_candidate(
    candidate: ProspectCandidate,
    existing_candidates: list[ProspectCandidate],
    *,
    fuzzy_review_threshold: float = 85.0,
) -> CandidateMatch:
    """Only exact matches merge; fuzzy matches always require human review."""
    exact_matches: list[tuple[int, str, ProspectCandidate]] = []
    blocked_matches: list[tuple[str, str, ProspectCandidate]] = []
    for existing in existing_candidates:
        key = _exact_key(candidate, existing)
        if key:
            conflict = _blocking_higher_priority_conflict(candidate, existing, key)
            if conflict:
                blocked_matches.append((conflict, key, existing))
            else:
                exact_matches.append((_EXACT_PRIORITY[key], key, existing))

    if blocked_matches:
        involved = [existing for _, _, existing in blocked_matches]
        involved.extend(existing for _, _, existing in exact_matches)
        entity_keys = {
            existing.candidate_id or f"unidentified:{id(existing)}"
            for existing in involved
        }
        conflict, key, existing = min(
            blocked_matches,
            key=lambda item: (
                _EXACT_PRIORITY[item[0]],
                _EXACT_PRIORITY[item[1]],
            ),
        )
        return CandidateMatch(
            disposition=DedupDisposition.possible_duplicate,
            matched_id=existing.candidate_id if len(entity_keys) == 1 else None,
            key=f"conflicting_{conflict}_blocks_{key}",
            score=100.0,
        )

    if exact_matches:
        entity_keys = {
            existing.candidate_id or f"unidentified:{id(existing)}"
            for _, _, existing in exact_matches
        }
        if len(entity_keys) > 1:
            return CandidateMatch(
                disposition=DedupDisposition.possible_duplicate,
                key="ambiguous_exact_identifiers",
                score=100.0,
            )
        _, key, existing = min(exact_matches, key=lambda item: item[0])
        return CandidateMatch(
            disposition=DedupDisposition.exact_match,
            matched_id=existing.candidate_id,
            key=key,
            score=100.0,
        )

    best: tuple[ProspectCandidate, float] | None = None
    for existing in existing_candidates:
        score = _fuzzy_score(candidate, existing)
        if best is None or score > best[1]:
            best = (existing, score)
    if best and best[1] >= fuzzy_review_threshold:
        return CandidateMatch(
            disposition=DedupDisposition.possible_duplicate,
            matched_id=best[0].candidate_id,
            key="fuzzy_name_comuna",
            score=best[1],
        )
    return CandidateMatch(disposition=DedupDisposition.unique)


def merge_exact_candidate(
    canonical: ProspectCandidate, incoming: ProspectCandidate
) -> ProspectCandidate:
    """Add evidence/provider ids while preserving canonical business fields."""
    match_key = _exact_key(canonical, incoming)
    if match_key is None:
        raise ValueError("candidates do not have an exact identity match")
    conflict = _blocking_higher_priority_conflict(canonical, incoming, match_key)
    if conflict:
        raise ValueError(
            f"conflicting {conflict} blocks exact merge by {match_key}"
        )
    provider_ids = canonical.provider_ids | incoming.provider_ids

    def location_key(location) -> tuple[str, str, str]:
        return (
            normalize_geo(location.region_code or location.region_name),
            normalize_geo(location.comuna_code or location.comuna_name),
            normalize_address(location.address) or "",
        )

    locations = {
        location_key(location): location for location in [*canonical.locations, *incoming.locations]
    }
    merged_locations = list(locations.values())
    merged_index = {
        location_key(location): index for index, location in enumerate(merged_locations)
    }

    def remap_indexed_evidence(source: ProspectCandidate):
        for evidence in source.evidence:
            match = _INDEXED_LOCATION_FIELD.fullmatch(evidence.field)
            if not match:
                yield evidence
                continue
            old_index = int(match.group(1))
            if old_index >= len(source.locations):
                continue
            new_index = merged_index[location_key(source.locations[old_index])]
            yield evidence.model_copy(
                update={"field": f"locations[{new_index}].{match.group(2)}"}
            )

    evidence = compact_evidence(
        [
            *remap_indexed_evidence(canonical),
            *remap_indexed_evidence(incoming),
        ]
    )
    updates: dict = {
        "provider_ids": provider_ids,
        "evidence": evidence,
        "dedup_disposition": DedupDisposition.exact_match,
    }

    updates["locations"] = merged_locations
    if incoming.score is not None:
        updates["score"] = max(canonical.score or 0, incoming.score)
    for field in ("rut", "phone", "email", "website", "description", "category"):
        if not getattr(canonical, field) and getattr(incoming, field):
            updates[field] = getattr(incoming, field)
    return canonical.model_copy(update=updates)
