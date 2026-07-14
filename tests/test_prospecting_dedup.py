from datetime import datetime, timedelta, timezone

import pytest

from app.prospecting.contracts import (
    DedupDisposition,
    ProspectCandidate,
    ProspectLocation,
    SourceEvidence,
    SourceName,
)
from app.prospecting.dedup import match_candidate, merge_exact_candidate
from app.prospecting.store import stable_candidate_id


def prospect(candidate_id: str, **updates) -> ProspectCandidate:
    fields = {
        "candidate_id": candidate_id,
        "name": "Clima Andes SpA",
        "website": "https://clima-andes.cl",
        "location": ProspectLocation(comuna_code="13114", comuna_name="Las Condes"),
    }
    fields.update(updates)
    return ProspectCandidate(**fields)


@pytest.mark.parametrize(
    ("existing_updates", "incoming_updates", "expected_key"),
    [
        ({"rut": "12.345.678-5"}, {"rut": "12345678-5"}, "rut"),
        (
            {"provider_ids": {"google_places": "place-1"}, "website": None},
            {"provider_ids": {"google_places": "place-1"}, "website": None},
            "provider_id",
        ),
        ({}, {}, "domain"),
        (
            {"website": None, "phone": "+56987654321"},
            {"website": None, "phone": "9 8765 4321"},
            "phone",
        ),
        (
            {"website": None, "name": "Clima Sur"},
            {"website": None, "name": "CLIMA SUR LTDA."},
            "name_comuna",
        ),
    ],
)
def test_exact_dedup_priority(existing_updates, incoming_updates, expected_key) -> None:
    result = match_candidate(
        prospect("existing", **incoming_updates),
        [prospect("existing", **existing_updates)],
    )
    assert result.disposition == DedupDisposition.exact_match
    assert result.key == expected_key


def test_fuzzy_match_never_auto_merges() -> None:
    result = match_candidate(
        prospect("incoming", website=None, name="Climatizacion Central"),
        [prospect("existing", website=None, name="Climatizac Central")],
        fuzzy_review_threshold=75,
    )
    assert result.disposition == DedupDisposition.possible_duplicate
    assert result.matched_id == "existing"


@pytest.mark.parametrize("reverse", [False, True])
def test_global_exact_priority_is_independent_of_existing_order(reverse) -> None:
    domain_match = prospect(
        "same-entity", rut=None, website="https://shared-domain.cl"
    )
    rut_match = prospect(
        "same-entity", rut="12.345.678-5", website="https://rut-entity.cl"
    )
    existing = [domain_match, rut_match]
    if reverse:
        existing.reverse()
    incoming = prospect(
        "incoming", rut="12.345.678-5", website="https://shared-domain.cl"
    )

    result = match_candidate(incoming, existing)

    assert result.disposition == DedupDisposition.exact_match
    assert result.matched_id == "same-entity"
    assert result.key == "rut"


def test_conflicting_exact_identifiers_are_ambiguous_and_never_auto_merged() -> None:
    existing = [
        prospect("rut-entity", rut="12.345.678-5", website="https://rut-only.cl"),
        prospect("domain-entity", rut=None, website="https://shared-domain.cl"),
    ]
    incoming = prospect(
        "incoming", rut="12.345.678-5", website="https://shared-domain.cl"
    )

    result = match_candidate(incoming, existing)

    assert result.disposition == DedupDisposition.possible_duplicate
    assert result.matched_id is None
    assert result.key == "ambiguous_exact_identifiers"


@pytest.mark.parametrize(
    ("existing_updates", "incoming_updates", "conflict", "lower_match"),
    [
        (
            {"rut": "12.345.678-5", "website": None, "phone": "+56987654321"},
            {"rut": "76.543.210-3", "website": None, "phone": "9 8765 4321"},
            "rut",
            "phone",
        ),
        (
            {"rut": "12.345.678-5", "website": "https://dominio-compartido.cl"},
            {"rut": "76.543.210-3", "website": "https://dominio-compartido.cl"},
            "rut",
            "domain",
        ),
        (
            {
                "provider_ids": {"google_places": "place-a"},
                "website": "https://dominio-compartido.cl",
            },
            {
                "provider_ids": {"google_places": "place-b"},
                "website": "https://dominio-compartido.cl",
            },
            "provider_id",
            "domain",
        ),
        (
            {"website": "https://empresa-a.cl", "phone": "+56987654321"},
            {"website": "https://empresa-b.cl", "phone": "9 8765 4321"},
            "domain",
            "phone",
        ),
    ],
)
def test_higher_priority_conflict_blocks_lower_exact_match(
    existing_updates,
    incoming_updates,
    conflict,
    lower_match,
) -> None:
    existing = prospect("existing", **existing_updates)
    incoming = prospect("incoming", **incoming_updates)

    result = match_candidate(incoming, [existing])

    assert result.disposition == DedupDisposition.possible_duplicate
    assert result.matched_id == "existing"
    assert result.key == f"conflicting_{conflict}_blocks_{lower_match}"
    with pytest.raises(ValueError, match=f"conflicting {conflict}"):
        merge_exact_candidate(existing, incoming)


def test_name_comuna_with_same_normalized_address_is_exact() -> None:
    result = match_candidate(
        prospect(
            "incoming",
            website=None,
            location=ProspectLocation(
                comuna_code="13114", comuna_name="Las Condes", address="Avenida Apoquindo 123"
            ),
        ),
        [
            prospect(
                "existing",
                website=None,
                location=ProspectLocation(
                    comuna_code="13114", comuna_name="Las Condes", address="Av. Apoquindo 123"
                ),
            )
        ],
    )

    assert result.disposition == DedupDisposition.exact_match
    assert result.key == "name_comuna_address"


def test_name_comuna_with_only_one_address_requires_review() -> None:
    result = match_candidate(
        prospect(
            "incoming",
            website=None,
            location=ProspectLocation(
                comuna_code="13114", comuna_name="Las Condes", address="Av. Apoquindo 123"
            ),
        ),
        [prospect("existing", website=None)],
    )

    assert result.disposition == DedupDisposition.possible_duplicate


def test_name_comuna_with_different_addresses_never_collides_or_auto_merges() -> None:
    incoming = prospect(
        "incoming",
        website=None,
        location=ProspectLocation(
            comuna_code="13114", comuna_name="Las Condes", address="Av. Apoquindo 123"
        ),
    )
    existing = prospect(
        "existing",
        website=None,
        location=ProspectLocation(
            comuna_code="13114", comuna_name="Las Condes", address="Av. Apoquindo 999"
        ),
    )

    result = match_candidate(incoming, [existing])

    assert result.disposition == DedupDisposition.possible_duplicate
    assert stable_candidate_id(incoming) != stable_candidate_id(existing)


def test_exact_company_match_preserves_two_distinct_branch_locations() -> None:
    canonical = prospect(
        "existing",
        location=ProspectLocation(
            region_code="13",
            comuna_code="13101",
            comuna_name="Santiago",
            address="Av. Providencia 100",
        ),
    )
    incoming = prospect(
        "incoming",
        location=ProspectLocation(
            region_code="13",
            comuna_code="13114",
            comuna_name="Las Condes",
            address="Av. Apoquindo 1234",
        ),
    )

    merged = merge_exact_candidate(canonical, incoming)

    assert len(merged.locations) == 2
    assert {location.comuna_code for location in merged.locations} == {"13101", "13114"}


def test_repeated_keyword_observations_are_compacted_below_edge_limit() -> None:
    observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    canonical = prospect("existing")
    for index in range(20):
        provider = (
            SourceName.brave_search if index % 2 == 0 else SourceName.official_website
        )
        record_id = "brave-record" if provider == SourceName.brave_search else "official.cl"
        items = [
            SourceEvidence(
                provider=provider,
                provider_record_id=record_id,
                field=field,
                value=value,
                observed_at=observed + timedelta(minutes=index),
            )
            for field, value in (
                ("name", "Clima Andes SpA"),
                ("website", "https://clima-andes.cl"),
                ("location.comuna_code", "13114"),
                ("location.comuna_name", "Las Condes"),
            )
        ]
        incoming = prospect(f"incoming-{index}", evidence=items)
        canonical = merge_exact_candidate(canonical, incoming)

    assert len(canonical.evidence) <= 100
    assert {item.provider for item in canonical.evidence} == {
        SourceName.brave_search,
        SourceName.official_website,
    }
    assert {item.field for item in canonical.evidence} >= {
        "name",
        "website",
        "location.comuna_code",
        "location.comuna_name",
    }
