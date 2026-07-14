from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.prospecting.contracts import (
    ClaimedRun,
    ClaimedTask,
    ProspectCandidate,
    ProspectLocation,
    ProspectingCampaign,
    ProspectingRunSnapshot,
    RunEvent,
    SourceEvidence,
    SourceName,
    Territory,
)
from app.prospecting.store import MemoryWorkerStore, expand_task_specs


def territory(code: str, name: str) -> Territory:
    return Territory(
        region_code="13",
        region_name="Metropolitana de Santiago",
        comuna_code=code,
        comuna_name=name,
    )


def test_task_expansion_is_discovery_source_keyword_comuna_product() -> None:
    campaign = ProspectingCampaign(
        crm_campaign_id="campaign-1",
        name="Instaladores RM",
        territories=(territory("13101", "Santiago"), territory("13114", "Las Condes")),
        keywords=("climatización", "aire acondicionado"),
        sources=(
            SourceName.google_places,
            SourceName.brave_search,
            SourceName.official_website,
        ),
    )
    snapshot = ProspectingRunSnapshot(
        crm_run_id="run-1", campaign_version=1, campaign=campaign, requested_by="admin"
    )

    tasks = expand_task_specs(snapshot)

    assert len(tasks) == 8
    assert {task.source for task in tasks} == {
        SourceName.google_places,
        SourceName.brave_search,
    }
    assert {task.comuna_code for task in tasks} == {"13101", "13114"}


def test_territory_requires_official_cut_codes_and_matching_prefix() -> None:
    with pytest.raises(ValidationError):
        Territory(
            region_code="RM",
            region_name="Metropolitana de Santiago",
            comuna_code="13101",
            comuna_name="Santiago",
        )
    with pytest.raises(ValidationError, match="does not belong"):
        Territory(
            region_code="05",
            region_name="Valparaíso",
            comuna_code="13101",
            comuna_name="Santiago",
        )


def test_campaign_requires_an_authorized_discovery_source() -> None:
    with pytest.raises(ValidationError, match="discovery"):
        ProspectingCampaign(
            crm_campaign_id="campaign-1",
            name="Invalid",
            territories=(territory("13101", "Santiago"),),
            keywords=("climatización",),
            sources=(SourceName.official_website,),
        )


def test_brave_requires_official_website_for_territorial_verification() -> None:
    with pytest.raises(ValidationError, match="requires official_website"):
        ProspectingCampaign(
            crm_campaign_id="campaign-brave-unverified",
            name="Invalid Brave",
            territories=(territory("13101", "Santiago"),),
            keywords=("climatización",),
            sources=(SourceName.brave_search,),
        )

    google_only = ProspectingCampaign(
        crm_campaign_id="campaign-google-only",
        name="Google discovery",
        territories=(territory("13101", "Santiago"),),
        keywords=("climatización",),
        sources=(SourceName.google_places,),
    )
    assert google_only.sources == (SourceName.google_places,)


def test_evidence_requires_traceability_and_google_gets_retention() -> None:
    with pytest.raises(ValidationError, match="source_url or provider_record_id"):
        SourceEvidence(provider=SourceName.brave_search, field="name", value="Clima")

    evidence = SourceEvidence(
        provider=SourceName.google_places,
        provider_record_id="ChIJ123",
        field="name",
        value="Clima SpA",
    )
    assert evidence.retention_until == evidence.observed_at + timedelta(days=30)


def test_outbox_contract_rejects_values_larger_than_edge_limits() -> None:
    with pytest.raises(ValidationError):
        ProspectCandidate(
            name="Clima",
            provider_ids={"x" * 41: "provider-id"},
            location=ProspectLocation(region_code="13", comuna_code="13101"),
        )
    with pytest.raises(ValidationError):
        SourceEvidence(
            provider=SourceName.brave_search,
            source_url=f"https://example.cl/{'x' * 2040}",
            field="description",
            value="ok",
        )
    with pytest.raises(ValidationError):
        SourceEvidence(
            provider=SourceName.brave_search,
            provider_record_id="record",
            field="description",
            value="x" * 4001,
        )
    with pytest.raises(ValidationError):
        RunEvent(
            event_id="event",
            run_id="run",
            stage="test",
            message="x" * 2001,
        )


def test_target_types_are_preserved_and_validated() -> None:
    campaign = ProspectingCampaign(
        crm_campaign_id="campaign-types",
        name="Técnicos",
        territories=(territory("13101", "Santiago"),),
        keywords=("refrigeración",),
        sources=(SourceName.google_places,),
        target_types=("tecnico", "instalador grande"),
    )
    assert campaign.model_dump()["target_types"] == ("tecnico", "instalador grande")

    with pytest.raises(ValidationError):
        ProspectingCampaign(
            crm_campaign_id="campaign-invalid-type",
            name="Invalid",
            territories=(territory("13101", "Santiago"),),
            keywords=("refrigeración",),
            sources=(
                SourceName.google_places,
                SourceName.brave_search,
                SourceName.official_website,
            ),
            target_types=("tipo inventado",),
        )


@pytest.mark.asyncio
async def test_explicit_empty_crm_tasks_never_expand_local_ids() -> None:
    campaign = ProspectingCampaign(
        crm_campaign_id="campaign-empty",
        name="No remaining tasks",
        territories=(territory("13101", "Santiago"),),
        keywords=("climatización",),
        sources=(SourceName.google_places,),
    )
    snapshot = ProspectingRunSnapshot(
        crm_run_id="run-empty", campaign_version=1, campaign=campaign, requested_by="admin"
    )
    claim = ClaimedRun(
        snapshot=snapshot,
        lease_token="lease",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        tasks=(),
    )
    store = MemoryWorkerStore()

    run_id = await store.ensure_run(claim, task_max_attempts=3)

    assert store.runs[run_id].tasks == {}


@pytest.mark.asyncio
async def test_fresh_local_store_recovers_authoritative_terminal_task_state() -> None:
    snapshot = ProspectingRunSnapshot(
        crm_run_id="226cc7cb-39d7-4c69-8d2c-826db6fc65b3",
        campaign_version=1,
        requested_by="e6e23313-b8e8-4a47-9170-5cb6e09e3f27",
        campaign=ProspectingCampaign(
            crm_campaign_id="e796f350-3474-4b62-9f56-2ba9e5ead614",
            name="Recovery",
            territories=(territory("13101", "Santiago"),),
            keywords=("climatización",),
            sources=(
                SourceName.google_places,
                SourceName.brave_search,
                SourceName.official_website,
            ),
        ),
    )
    claim = ClaimedRun(
        snapshot=snapshot,
        lease_token="lease-recovery",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        candidates_found=7,
        tasks=(
            ClaimedTask(
                id="f6fc2fc5-754c-41b6-b437-99b4c9bd173f",
                source=SourceName.brave_search,
                keyword="climatización",
                region_code="13",
                comuna_code="13101",
                status="completed",
                attempts=2,
                candidates_found=7,
            ),
            ClaimedTask(
                id="91f16fcb-f948-4c5e-a472-195fd3d84761",
                source=SourceName.google_places,
                keyword="climatización",
                region_code="13",
                comuna_code="13101",
                status="failed",
                attempts=3,
                max_attempts=3,
            ),
        ),
    )
    store = MemoryWorkerStore()

    run_id = await store.ensure_run(claim, task_max_attempts=3)
    summary = await store.summary(run_id)

    assert summary.completed == 1
    assert summary.failed == 1
    assert summary.candidates == 7
    assert await store.claim_task(run_id, "worker-recovery", 120) is None


@pytest.mark.asyncio
async def test_recovered_task_at_max_attempts_is_failed_without_requery() -> None:
    snapshot = ProspectingRunSnapshot(
        crm_run_id="f69ba891-0043-456f-9300-59c67282dd1a",
        campaign_version=1,
        requested_by="e6e23313-b8e8-4a47-9170-5cb6e09e3f27",
        campaign=ProspectingCampaign(
            crm_campaign_id="e796f350-3474-4b62-9f56-2ba9e5ead614",
            name="Recovery max attempts",
            territories=(territory("13101", "Santiago"),),
            keywords=("climatización",),
            sources=(SourceName.google_places,),
        ),
    )
    task_id = "56000fb6-af9e-49cb-a8fd-d3165b104211"
    claim = ClaimedRun(
        snapshot=snapshot,
        lease_token="lease-recovery",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        tasks=(
            ClaimedTask(
                id=task_id,
                source=SourceName.google_places,
                keyword="climatización",
                region_code="13",
                comuna_code="13101",
                status="running",
                attempts=3,
                max_attempts=3,
            ),
        ),
    )
    store = MemoryWorkerStore()

    run_id = await store.ensure_run(claim, task_max_attempts=3)

    assert await store.claim_task(run_id, "worker-recovery", 120) is None
    assert (await store.summary(run_id)).failed == 1
