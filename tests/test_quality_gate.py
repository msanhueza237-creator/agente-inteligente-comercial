import pytest

from app.prospecting.contracts import (
    DerivedProvenance,
    ProspectCandidate,
    ProspectLocation,
    ProspectingCampaign,
    ProspectingRunSnapshot,
    SourceEvidence,
    SourceName,
    Territory,
)
from app.prospecting.scoring import classify_and_score, infer_target_type
from app.prospecting.validation import (
    normalize_geo,
    sanitize_unsubstantiated_external_fields,
    validate_candidate,
)


@pytest.fixture
def snapshot() -> ProspectingRunSnapshot:
    return ProspectingRunSnapshot(
        crm_run_id="run-quality",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign-quality",
            name="HVAC RM",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13114",
                    comuna_name="Las Condes",
                ),
            ),
            keywords=("climatización",),
            sources=(SourceName.brave_search, SourceName.official_website),
        ),
    )


def candidate(**updates) -> ProspectCandidate:
    base = {
        "name": "Climatización Andes SpA",
        "website": "https://clima-andes.cl",
        "description": "Instalación de aire acondicionado y refrigeración",
        "category": "tecnico",
        "score": 75,
        "derived_provenance": {
            "category": DerivedProvenance(ruleset="test_hvac_classification_v1"),
            "score": DerivedProvenance(ruleset="test_commercial_score_v1"),
        },
        "location": ProspectLocation(
            region_code="13",
            region_name="Región Metropolitana de Santiago",
            comuna_code="13114",
            comuna_name="Las Condes",
        ),
    }
    evidence_was_supplied = "evidence" in updates
    base.update(updates)
    prospect = ProspectCandidate(**base)
    if evidence_was_supplied:
        return prospect

    values = [
        ("name", prospect.name),
        *(
            (field_name, getattr(prospect, field_name))
            for field_name in ("rut", "trade_name", "phone", "email", "website", "description")
        ),
    ]
    for location in prospect.locations:
        values.extend(
            (
                ("location.region_code", location.region_code),
                ("location.region_name", location.region_name),
                ("location.comuna_code", location.comuna_code),
                ("location.comuna_name", location.comuna_name),
                ("location.address", location.address),
            )
        )
    evidence = [
        SourceEvidence(
            provider=SourceName.brave_search,
            source_url="https://clima-andes.cl",
            field=field_name,
            value=value,
        )
        for field_name, value in values
        if value
    ]
    return prospect.model_copy(update={"evidence": evidence})


def test_quality_gate_accepts_only_hvac_geo_contact_with_evidence(snapshot) -> None:
    assert validate_candidate(candidate(), snapshot).accepted


def test_unsubstantiated_fields_are_scored_then_stripped_before_send(snapshot) -> None:
    raw = candidate(phone="+56 9 8765 4321")
    raw = raw.model_copy(
        update={
            "evidence": [
                evidence
                for evidence in raw.evidence
                if evidence.field not in {"phone", "description", "location.region_name"}
            ]
        }
    )

    scored = classify_and_score(raw, snapshot)
    sanitized = sanitize_unsubstantiated_external_fields(scored)

    assert sanitized.score == scored.score
    assert sanitized.phone is None
    assert sanitized.description is None
    assert sanitized.location.region_name is None
    assert sanitized.location.region_code == "13"
    assert {"category", "score"}.issubset(sanitized.derived_provenance)
    assert validate_candidate(sanitized, snapshot).accepted


def test_quality_gate_enforces_target_types(snapshot) -> None:
    restricted = snapshot.model_copy(
        update={"campaign": snapshot.campaign.model_copy(update={"target_types": ("tecnico",)})}
    )
    result = validate_candidate(candidate(category="distribuidor"), restricted)
    assert "outside_target_types" in result.reasons


def test_market_signals_classify_and_prioritize_replacement_distributor(snapshot) -> None:
    prospect = candidate(
        name="Acondipart Repuestos e Insumos HVAC",
        description="Mayorista e importador de repuestos para refrigeración y aire acondicionado",
        specialties=("aire acondicionado", "refrigeracion"),
        brands=("Daikin", "Copeland"),
        category=None,
    )

    prepared = classify_and_score(prospect, snapshot)

    assert infer_target_type(prospect) == "distribuidor"
    assert prepared.category == "distribuidor"
    assert prepared.score is not None and prepared.score >= 75


def test_market_score_rewards_repeated_discovery_and_commercial_reach(snapshot) -> None:
    prospect = candidate(
        name="Distribuidora HVAC Nacional",
        description="Mayorista importador de repuestos de refrigeracion",
        website="https://distribuidor-hvac.cl",
        phone="+56223456789",
        brands=("Copeland", "Danfoss", "Emerson"),
        market_signals={"query_hits": 5, "best_rank": 2, "radar_mode": True},
    )

    prepared = classify_and_score(prospect, snapshot)

    assert prepared.market_score is not None and prepared.market_score >= 70
    assert prepared.market_score != prepared.score


def test_google_generic_type_is_rejected_without_hvac_evidence(snapshot) -> None:
    restricted = snapshot.model_copy(
        update={"campaign": snapshot.campaign.model_copy(update={"target_types": ("tecnico",)})}
    )
    raw = candidate(
        name="Servicios Andes SpA",
        description="store point_of_interest establishment",
        category=None,
        provider_ids={"google_places": "place-generic"},
        review_flags=("hvac_query_match", "hvac_relevance_needs_review"),
    )

    prepared = classify_and_score(raw, restricted)
    result = validate_candidate(prepared, restricted)

    assert prepared.category == "otro"
    assert "target_type_unconfirmed" in prepared.review_flags
    assert "not_hvac_related" in result.reasons
    assert not result.accepted


def test_google_generic_type_is_rescued_by_official_hvac_specialties(snapshot) -> None:
    raw = candidate(
        name="Servicios Andes SpA",
        description="store point_of_interest establishment",
        category=None,
        specialties=("aire acondicionado", "mantencion"),
        provider_ids={"google_places": "place-generic"},
        review_flags=("hvac_query_match", "hvac_relevance_needs_review"),
    )

    prepared = classify_and_score(raw, snapshot)
    result = validate_candidate(prepared, snapshot)

    assert result.accepted


def test_quality_gate_rejects_when_any_branch_is_outside_campaign(snapshot) -> None:
    outside = ProspectLocation(
        region_code="05",
        region_name="Valparaíso",
        comuna_code="05101",
        comuna_name="Valparaíso",
    )
    result = validate_candidate(
        candidate(locations=[outside]),
        snapshot,
    )
    assert "outside_requested_territory" in result.reasons


@pytest.mark.parametrize(
    "google_type",
    [
        "air_conditioning_contractor",
        "hvac_contractor",
        "heating_contractor",
        "refrigeration",
    ],
)
def test_quality_gate_accepts_official_google_hvac_types(snapshot, google_type) -> None:
    assert validate_candidate(
        candidate(name="Servicios Técnicos Andes", description=google_type), snapshot
    ).accepted


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"description": "Venta de artículos de oficina", "name": "Comercial Andes"},
            "not_hvac_related",
        ),
        (
            {
                "location": ProspectLocation(
                    region_code="05",
                    region_name="Valparaíso",
                    comuna_code="05101",
                    comuna_name="Valparaíso",
                )
            },
            "outside_requested_territory",
        ),
        ({"website": None}, "missing_business_contact"),
        ({"evidence": []}, "missing_required_evidence"),
    ],
)
def test_quality_gate_rejection_reasons(snapshot, changes, reason) -> None:
    assert reason in validate_candidate(candidate(**changes), snapshot).reasons


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Región Metropolitana de Santiago", "RM"),
        ("Región del Libertador General Bernardo O'Higgins", "O'Higgins"),
        ("Región de La Araucanía", "Araucanía"),
        ("Región de Ñuble", "Ñuble"),
        ("Región de Aysén del General Carlos Ibáñez del Campo", "Aysén"),
        ("Región de Magallanes y de la Antártica Chilena", "Magallanes"),
    ],
)
def test_chilean_region_aliases_are_canonical(left, right) -> None:
    assert normalize_geo(left) == normalize_geo(right)
