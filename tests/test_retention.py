from datetime import datetime, timedelta, timezone

from app.prospecting.contracts import (
    ProspectCandidate,
    ProspectLocation,
    ProspectingCampaign,
    ProspectingRunSnapshot,
    SourceEvidence,
    SourceName,
    Territory,
)
from app.prospecting.retention import (
    rehydrate_candidate_batch_payload,
    rehydrate_candidate_from_evidence,
)
from app.prospecting.store import scope_candidate_locations


OBSERVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot() -> ProspectingRunSnapshot:
    return ProspectingRunSnapshot(
        crm_run_id="run-retention",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign-retention",
            name="Retention",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13101",
                    comuna_name="Santiago",
                ),
            ),
            keywords=("climatización",),
            sources=(SourceName.brave_search, SourceName.official_website),
        ),
    )


def evidence(
    provider: SourceName,
    field: str,
    value: str,
    record_id: str,
) -> SourceEvidence:
    return SourceEvidence(
        provider=provider,
        provider_record_id=record_id,
        source_url=f"https://source.test/{record_id}",
        field=field,
        value=value,
        observed_at=OBSERVED_AT,
    )


def two_branch_candidate(branch_provider: SourceName) -> ProspectCandidate:
    hq = ProspectLocation(
        region_code="13",
        region_name="Región Metropolitana de Santiago",
        comuna_code="13101",
        comuna_name="Santiago",
        address="Av. Principal 100",
    )
    branch = ProspectLocation(
        region_code="13",
        region_name="Región Metropolitana de Santiago",
        comuna_code="13101",
        comuna_name="Santiago",
        address="Av. Sucursal 900",
    )
    items = [
        evidence(SourceName.brave_search, "name", "Climatización Dos Sedes SpA", "hq"),
        evidence(
            SourceName.brave_search,
            "website",
            "https://dos-sedes.cl",
            "hq",
        ),
        evidence(
            SourceName.google_places,
            "description",
            "Instalación y mantención de aire acondicionado",
            "google-description",
        ),
        evidence(
            SourceName.google_places,
            "phone",
            "+56 9 8765 4321",
            "google-phone",
        ),
    ]
    for location, provider, record_id in (
        (hq, SourceName.brave_search, "hq"),
        (branch, branch_provider, "branch"),
    ):
        for attribute in (
            "region_code",
            "region_name",
            "comuna_code",
            "comuna_name",
            "address",
        ):
            items.append(
                evidence(
                    provider,
                    f"location.{attribute}",
                    getattr(location, attribute),
                    record_id,
                )
            )
    return ProspectCandidate(
        candidate_id="candidate-two-branches",
        name="Climatización Dos Sedes SpA",
        provider_ids={
            "brave_search": "hq",
            **(
                {"google_places": "branch"}
                if branch_provider == SourceName.google_places
                else {}
            ),
        },
        phone="+56 9 8765 4321",
        website="https://dos-sedes.cl",
        description="Instalación y mantención de aire acondicionado",
        location=hq,
        locations=[hq, branch],
        evidence=items,
    )


def retained_after_31_days(candidate: ProspectCandidate) -> list[SourceEvidence]:
    at = OBSERVED_AT + timedelta(days=31)
    return [
        item
        for item in candidate.evidence
        if item.retention_until is None or item.retention_until > at
    ]


def test_two_official_locations_are_indexed_across_distinct_observation_times() -> None:
    original = two_branch_candidate(SourceName.brave_search)
    items: list[SourceEvidence] = []
    values = [
        ("name", original.name),
        ("website", original.website),
        ("description", original.description),
        ("phone", original.phone),
    ]
    for location in original.locations:
        values.extend(
            (f"location.{attribute}", getattr(location, attribute))
            for attribute in (
                "region_code",
                "region_name",
                "comuna_code",
                "comuna_name",
                "address",
            )
        )
    for offset, (field, value) in enumerate(values):
        items.append(
            SourceEvidence(
                provider=SourceName.official_website,
                provider_record_id="official-contact-page",
                source_url="https://dos-sedes.cl/contacto",
                field=field,
                value=value,
                observed_at=OBSERVED_AT + timedelta(microseconds=offset),
            )
        )
    candidate = original.model_copy(
        update={
            "provider_ids": {"official_website": "official-contact-page"},
            "evidence": items,
        }
    )

    prepared = scope_candidate_locations(candidate, snapshot())
    indexed_fields = {item.field for item in prepared.evidence}

    for index in range(2):
        assert {
            f"locations[{index}].region_code",
            f"locations[{index}].comuna_code",
            f"locations[{index}].address",
        }.issubset(indexed_fields)
    assert prepared.importable_location_indexes == (0, 1)


def test_google_only_candidate_is_removed_after_retention_window() -> None:
    original = two_branch_candidate(SourceName.google_places)
    google_items = [
        evidence(SourceName.google_places, item.field, item.value, "google-only")
        for item in original.evidence
    ]
    google_only = original.model_copy(
        update={"provider_ids": {"google_places": "google-only"}, "evidence": google_items}
    )
    prepared = scope_candidate_locations(google_only, snapshot())

    assert prepared.import_eligible
    assert "contact_only_import" in prepared.review_flags
    assert rehydrate_candidate_from_evidence(
        prepared, retained_after_31_days(prepared)
    ) is None
    assert rehydrate_candidate_batch_payload(
        [prepared.model_dump(mode="json")],
        at=OBSERVED_AT + timedelta(days=31),
    ) == []


def test_mixed_candidate_rehydrates_all_fields_and_keeps_only_brave_hq() -> None:
    prepared = scope_candidate_locations(
        two_branch_candidate(SourceName.google_places), snapshot()
    )

    assert prepared.importable_location_indexes == (0,)
    rebuilt = rehydrate_candidate_from_evidence(
        prepared, retained_after_31_days(prepared)
    )

    assert rebuilt is not None
    assert rebuilt.import_eligible
    assert len(rebuilt.locations) == 1
    assert rebuilt.location.address == "Av. Principal 100"
    assert rebuilt.phone is None
    assert rebuilt.description is None
    assert rebuilt.provider_ids == {"brave_search": "hq"}
    assert {item.provider for item in rebuilt.evidence} == {SourceName.brave_search}


def test_google_candidate_with_official_identity_address_and_email_survives() -> None:
    location = ProspectLocation(
        region_code="13",
        region_name="Región Metropolitana de Santiago",
        comuna_code="13101",
        comuna_name="Santiago",
        address="Av. Oficial 123",
    )
    official_items = [
        evidence(SourceName.official_website, "name", "Clima Oficial SpA", "oficial.cl"),
        evidence(
            SourceName.official_website,
            "email",
            "ventas@oficial.cl",
            "oficial.cl",
        ),
        evidence(
            SourceName.official_website,
            "website",
            "https://oficial.cl",
            "oficial.cl",
        ),
    ]
    for attribute in (
        "region_code",
        "region_name",
        "comuna_code",
        "comuna_name",
        "address",
    ):
        official_items.append(
            evidence(
                SourceName.official_website,
                f"location.{attribute}",
                getattr(location, attribute),
                "oficial.cl",
            )
        )
    candidate = ProspectCandidate(
        name="Clima Oficial SpA",
        provider_ids={"google_places": "google-official", "official_website": "oficial.cl"},
        email="ventas@oficial.cl",
        website="https://oficial.cl",
        description="air_conditioning_contractor",
        location=location,
        evidence=[
            *official_items,
            evidence(
                SourceName.google_places,
                "description",
                "air_conditioning_contractor",
                "google-official",
            ),
        ],
    )
    prepared = scope_candidate_locations(candidate, snapshot())

    rebuilt = rehydrate_candidate_from_evidence(
        prepared, retained_after_31_days(prepared)
    )

    assert rebuilt is not None
    assert rebuilt.import_eligible
    assert rebuilt.name == "Clima Oficial SpA"
    assert rebuilt.email == "ventas@oficial.cl"
    assert rebuilt.location.address == "Av. Oficial 123"
    assert rebuilt.description is None
    assert rebuilt.provider_ids == {"official_website": "oficial.cl"}


def test_two_brave_addresses_in_same_comuna_remain_two_importable_locations() -> None:
    prepared = scope_candidate_locations(
        two_branch_candidate(SourceName.brave_search), snapshot()
    )

    assert prepared.importable_location_indexes == (0, 1)
    rebuilt = rehydrate_candidate_from_evidence(
        prepared, retained_after_31_days(prepared)
    )

    assert rebuilt is not None
    assert len(rebuilt.locations) == 2
    assert {location.address for location in rebuilt.locations} == {
        "Av. Principal 100",
        "Av. Sucursal 900",
    }
    assert rebuilt.importable_location_indexes == (0, 1)
