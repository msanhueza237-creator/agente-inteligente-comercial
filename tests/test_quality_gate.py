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
from app.prospecting.quality import evaluate_hvac_quality
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
    evidence_provider = updates.pop("_evidence_provider", SourceName.official_website)
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
        ("category", prospect.category),
        ("specialties", " | ".join(prospect.specialties)),
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
            provider=evidence_provider,
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


@pytest.mark.parametrize(
    ("name", "description", "expected_type"),
    [
        (
            "Repuestos Clima Sur",
            "Tienda especializada en venta de repuestos para aire acondicionado",
            "tienda comercial",
        ),
        (
            "Distribuidora Frio Chile",
            "Importador mayorista de equipos e insumos HVAC-R",
            "distribuidor",
        ),
        (
            "Servicio Tecnico Andes",
            "Instalacion, mantencion y reparacion de aire acondicionado",
            "tecnico",
        ),
        (
            "Ingenieria Termica Chile",
            "Empresa de ingenieria y proyectos de climatizacion comercial",
            "instalador grande",
        ),
        (
            "Frio Industrial Sur",
            "Empresa de refrigeracion industrial e instalaciones comerciales",
            "instalador grande",
        ),
        (
            "Contratista Camaras Chile",
            "Contratista de camaras frigorificas y camaras de frio",
            "instalador grande",
        ),
    ],
)
def test_required_positive_hvac_matrix(
    snapshot, name, description, expected_type
) -> None:
    prospect = candidate(
        name=name,
        description=description,
        category=None,
        specialties=(),
        brands=(),
    )

    assessment = evaluate_hvac_quality(
        prospect,
        allowed_target_types=snapshot.campaign.target_types,
    )
    prepared = classify_and_score(prospect, snapshot)
    result = validate_candidate(prepared, snapshot)

    assert assessment.eligible
    assert assessment.inferred_target_type == expected_type
    assert assessment.hvac_confidence >= 0.9
    assert "official_website_hvac_evidence" in assessment.positive_signals
    assert prepared.category == expected_type
    assert result.accepted
    assert any(item.provider == SourceName.official_website for item in prospect.evidence)


@pytest.mark.parametrize(
    ("name", "description", "website", "expected_reason"),
    [
        (
            "Ferreteria Central",
            "Ferreteria general con herramientas y materiales de construccion",
            "https://ferreteria-central.cl",
            "excluded_business_type",
        ),
        (
            "Servicio Hogar",
            "Servicio tecnico de refrigeradores, lavadoras y linea blanca",
            "https://servicio-hogar.cl",
            "excluded_business_type",
        ),
        (
            "Auto Clima Chile",
            "Reparacion de aire acondicionado automotriz",
            "https://autoclima.cl",
            "excluded_business_type",
        ),
        (
            "Transportes Frio Sur",
            "Transporte refrigerado y camiones frigorificos",
            "https://transportes-frio.cl",
            "excluded_business_type",
        ),
        (
            "Supermercado La Plaza",
            "Supermercado con camaras de frio para sus alimentos",
            "https://supermercado-plaza.cl",
            "excluded_business_type",
        ),
        (
            "Hotel Restaurante Cordillera",
            "Hotel con climatizacion y camaras frigorificas propias",
            "https://hotel-cordillera.cl",
            "excluded_business_type",
        ),
        (
            "Empleos Tecnicos Chile",
            "Oferta de empleo para tecnico en climatizacion",
            "https://empleos-tecnicos.cl",
            "excluded_business_type",
        ),
        (
            "Academia Termica",
            "Curso de climatizacion y capacitacion HVAC",
            "https://academia-termica.cl",
            "excluded_business_type",
        ),
        (
            "Universidad Tecnica",
            "Universidad con diplomado de refrigeracion industrial",
            "https://universidad-tecnica.cl",
            "excluded_business_type",
        ),
        (
            "Blog Aire y Frio",
            "Blog de noticias sobre aire acondicionado",
            "https://blog-aire-frio.cl",
            "excluded_business_type",
        ),
        (
            "Directorio HVAC Chile",
            "Directorio de empresas de climatizacion",
            "https://www.amarillas.cl/hvac",
            "excluded_business_type",
        ),
        (
            "Marketplace Clima",
            "Venta de equipos de aire acondicionado",
            "https://www.mercadolibre.cl/aire-acondicionado",
            "excluded_business_type",
        ),
        (
            "Electricidad Integral",
            "Empresa de electricidad y mantencion general",
            "https://electricidad-integral.cl",
            "excluded_business_type",
        ),
        (
            "Clima Organizacional SpA",
            "Consultoria de personas y clima laboral",
            "https://clima-organizacional.cl",
            "not_hvac_related",
        ),
        (
            "Comercial Andes",
            "Venta de articulos de oficina con telefono y sitio web",
            "https://comercial-andes.cl",
            "not_hvac_related",
        ),
    ],
)
def test_required_negative_business_matrix(
    snapshot, name, description, website, expected_reason
) -> None:
    prospect = candidate(
        name=name,
        description=description,
        website=website,
        phone="+56223456789",
        email="ventas@empresa.cl",
        category=None,
        specialties=(),
        brands=(),
    )

    prepared = classify_and_score(prospect, snapshot)
    result = validate_candidate(prepared, snapshot)

    assert not result.accepted
    assert expected_reason in result.reasons
    assert prepared.score == 0


def test_query_match_alone_is_never_trade_evidence(snapshot) -> None:
    prospect = candidate(
        name="Servicios Comerciales Sur",
        description="Servicios para empresas",
        phone="+56223456789",
        category=None,
        specialties=(),
        brands=(),
        review_flags=("hvac_query_match", "hvac_relevance_needs_review"),
    )
    prepared = classify_and_score(prospect, snapshot)
    result = validate_candidate(prepared, snapshot)

    assert prepared.score == 0
    assert "not_hvac_related" in result.reasons
    assert not result.accepted


def test_other_is_never_accepted_automatically(snapshot) -> None:
    prospect = candidate(
        name="Soluciones de Climatizacion",
        description="Soluciones de climatizacion para empresas",
        category="otro",
        specialties=(),
        brands=(),
    )
    prepared = classify_and_score(prospect, snapshot)
    result = validate_candidate(prepared, snapshot)

    assert prepared.category == "otro"
    assert prepared.score == 0
    assert "target_type_unconfirmed" in result.reasons
    assert not result.accepted


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
        "refrigeration_equipment_supplier",
        "air_conditioning_equipment_store",
    ],
)
def test_quality_gate_accepts_official_google_hvac_types(snapshot, google_type) -> None:
    assert validate_candidate(
        candidate(
            name="Servicios Técnicos Andes",
            description=google_type,
            _evidence_provider=SourceName.google_places,
        ),
        snapshot,
    ).accepted


@pytest.mark.parametrize("google_type", ["heating_contractor", "refrigeration"])
def test_quality_gate_rejects_ambiguous_google_types_without_hvac_context(
    snapshot, google_type
) -> None:
    result = validate_candidate(
        candidate(
            name="Servicios Andes",
            description=google_type,
            category=None,
            specialties=(),
            _evidence_provider=SourceName.google_places,
        ),
        snapshot,
    )
    assert not result.accepted
    assert "not_hvac_related" in result.reasons


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
