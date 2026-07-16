from __future__ import annotations

import re

from unidecode import unidecode

from app.prospecting.contracts import (
    DerivedProvenance,
    ProspectCandidate,
    ProspectingRunSnapshot,
)


def infer_target_type(candidate: ProspectCandidate) -> str:
    text = " ".join(
        unidecode(value).lower()
        for value in (
            candidate.name,
            candidate.trade_name,
            candidate.description,
            candidate.category,
        )
        if value
    )
    text = re.sub(r"[_-]+", " ", text)
    if re.search(r"\b(distribuidor|importador|mayorista|distribucion)\b", text):
        return "distribuidor"
    if re.search(r"\b(tienda|retail|venta al detalle|e commerce|repuesto|insumo|suministro)\b", text):
        return "tienda comercial"
    if re.search(r"\b(competencia|competidor|proveedor)\b", text):
        return "competencia"
    if re.search(r"\b(industrial|ingenieria|grandes proyectos|proyectos comerciales)\b", text):
        return "instalador grande"
    if re.search(
        r"\b(tecnico|mantencion|mantenimiento|reparacion|instalacion|instalador|contractor|refrigeracion|refrigeration)\b",
        text,
    ):
        return "tecnico"
    return "otro"


def score_candidate(candidate: ProspectCandidate, snapshot: ProspectingRunSnapshot) -> float:
    score = 35.0
    if candidate.category in snapshot.campaign.target_types:
        score += 20
    if candidate.rut:
        score += 10
    if candidate.phone:
        score += 10
    if candidate.email:
        score += 10
    if candidate.website:
        score += 5
    providers = {evidence.provider for evidence in candidate.evidence}
    score += min(10, len(providers) * 5)
    text = " ".join(
        unidecode(value).lower()
        for value in (
            candidate.name,
            candidate.trade_name,
            candidate.description,
            candidate.category,
            *candidate.specialties,
        )
        if value
    )
    text = re.sub(r"[_-]+", " ", text)
    if re.search(r"\b(distribuidor|mayorista|importador)\b", text):
        score += 12
    elif re.search(r"\b(repuesto|insumo|proveedor|suministro)\b", text):
        score += 8
    if candidate.specialties:
        score += min(8, len(candidate.specialties) * 2)
    if candidate.brands:
        score += min(6, len(candidate.brands) * 2)
    if any(evidence.provider.value == "official_website" for evidence in candidate.evidence):
        score += 8
    return min(100.0, score)


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
    category = infer_target_type(candidate)
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
            ruleset="clima_activa_hvac_classification_v1",
            input_fields=("name", "trade_name", "description", "specialties"),
        ),
        "score": DerivedProvenance(
            ruleset="clima_activa_commercial_score_v1",
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
    market_score = market_importance_score(prepared)
    provenance["market_score"] = DerivedProvenance(
        ruleset="clima_activa_market_importance_v1",
        input_fields=("market_signals", "category", "brands", "locations", "evidence"),
    )
    return prepared.model_copy(update={"score": score, "market_score": market_score, "derived_provenance": provenance})
