from __future__ import annotations

from app.prospecting.contracts import (
    DerivedProvenance,
    ProspectCandidate,
    ProspectingRunSnapshot,
)
from app.prospecting.quality import evaluate_hvac_quality, infer_hvac_target_type


def _classification_text(candidate: ProspectCandidate) -> str:
    return " ".join(
        value
        for value in (
            candidate.name,
            candidate.trade_name,
            candidate.description,
            candidate.category,
            *candidate.specialties,
        )
        if value
    )


def infer_target_type(candidate: ProspectCandidate) -> str | None:
    """Infer only a supported HVAC-R commercial role.

    A generic refrigeration or maintenance token is deliberately not enough.
    """

    return infer_hvac_target_type(_classification_text(candidate))


def score_candidate(candidate: ProspectCandidate, snapshot: ProspectingRunSnapshot) -> float:
    assessment = evaluate_hvac_quality(
        candidate,
        allowed_target_types=snapshot.campaign.target_types,
        radar_mode="competencia" in snapshot.campaign.target_types,
    )
    if not assessment.eligible:
        return 0.0

    # Relevance is a gate and also the largest score component (max 45).
    score = assessment.hvac_confidence * 45
    if assessment.inferred_target_type in snapshot.campaign.target_types:
        score += 20

    permanent = [
        evidence
        for evidence in candidate.evidence
        if evidence.retention_until is None and evidence.confidence >= 0.7
    ]
    permanent_fields = {evidence.field for evidence in permanent}
    permanent_providers = {evidence.provider for evidence in permanent}
    score += min(15, len(permanent_fields) * 2 + len(permanent_providers) * 3)

    # Contact completeness cannot compensate for an irrelevant company (max 10).
    score += 4 if candidate.phone else 0
    score += 3 if candidate.email else 0
    score += 3 if candidate.website else 0

    # Observable scale/potential signals only (max 10).
    query_hits = int(candidate.market_signals.get("query_hits", 0) or 0)
    scale_score = min(4, query_hits)
    scale_score += min(3, len(candidate.brands))
    scale_score += min(3, max(0, len(candidate.locations) - 1))
    score += scale_score
    return min(100.0, round(score, 2))


def market_importance_score(candidate: ProspectCandidate) -> float:
    """Rank commercial reach separately from data completeness."""
    signals = candidate.market_signals
    query_hits = int(signals.get("query_hits", 0) or 0)
    best_rank = int(signals.get("best_rank", 20) or 20)
    score = min(30, query_hits * 6) + max(0, 18 - best_rank)
    score += min(12, len(candidate.brands) * 3)
    score += min(10, len(candidate.locations) * 3)
    if candidate.category in {"distribuidor", "tienda comercial", "competencia"}:
        score += 15
    if candidate.website:
        score += 5
    if candidate.phone or candidate.email:
        score += 5
    if any(evidence.provider.value == "official_website" for evidence in candidate.evidence):
        score += 10
    return min(100.0, float(score))


def classify_and_score(
    candidate: ProspectCandidate, snapshot: ProspectingRunSnapshot
) -> ProspectCandidate:
    assessment = evaluate_hvac_quality(
        candidate,
        allowed_target_types=snapshot.campaign.target_types,
        radar_mode="competencia" in snapshot.campaign.target_types,
    )
    category = assessment.inferred_target_type or "otro"
    review_flags = list(candidate.review_flags)
    if category == "otro" and "target_type_unconfirmed" not in review_flags:
        review_flags.append("target_type_unconfirmed")
    prepared = candidate.model_copy(
        update={"category": category, "review_flags": tuple(review_flags)}
    )
    score = score_candidate(prepared, snapshot)
    provenance = {
        **candidate.derived_provenance,
        "category": DerivedProvenance(
            ruleset="clima_activa_hvac_classification_v2",
            input_fields=("name", "trade_name", "description", "specialties"),
        ),
        "score": DerivedProvenance(
            ruleset="clima_activa_commercial_score_v2",
            input_fields=(
                "category",
                "rut",
                "phone",
                "email",
                "website",
                "evidence",
                "specialties",
                "brands",
            ),
        ),
    }
    market_score = market_importance_score(prepared) if assessment.eligible else 0.0
    provenance["market_score"] = DerivedProvenance(
        ruleset="clima_activa_market_importance_v1",
        input_fields=("market_signals", "category", "brands", "locations", "evidence"),
    )
    return prepared.model_copy(
        update={
            "score": score,
            "market_score": market_score,
            "derived_provenance": provenance,
        }
    )
