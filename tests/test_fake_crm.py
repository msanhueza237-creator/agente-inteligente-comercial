from datetime import timedelta
from uuid import UUID

import pytest

from app.crm.fake import FakeCRMPort, IdempotencyConflict, LeaseRejected
from app.prospecting.contracts import (
    ProspectCandidate,
    ProspectLocation,
    ProspectingCampaign,
    ProspectingRunSnapshot,
    RunEvent,
    SourceName,
    Territory,
)


def snapshot() -> ProspectingRunSnapshot:
    return ProspectingRunSnapshot(
        crm_run_id="run-fake",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign-fake",
            name="Fake",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13101",
                    comuna_name="Santiago",
                ),
            ),
            keywords=("HVAC",),
            sources=(SourceName.google_places,),
        ),
    )


@pytest.mark.asyncio
async def test_claim_is_exclusive_and_writes_require_lease() -> None:
    crm = FakeCRMPort()
    await crm.enqueue(snapshot())
    claim = await crm.claim_run("worker-a")
    assert claim is not None
    assert str(UUID(claim.lease_token)) == claim.lease_token
    assert await crm.claim_run("worker-b") is None

    event = RunEvent(event_id="event-1", run_id="run-fake", stage="test", message="ok")
    with pytest.raises(LeaseRejected):
        await crm.append_events("run-fake", "wrong", [event], "key-1")
    await crm.append_events("run-fake", claim.lease_token, [event], "key-1")
    await crm.append_events("run-fake", claim.lease_token, [event], "key-1")
    assert len((await crm.inspect_run("run-fake")).events) == 1


@pytest.mark.asyncio
async def test_idempotency_key_rejects_a_different_payload() -> None:
    crm = FakeCRMPort()
    await crm.enqueue(snapshot())
    claim = await crm.claim_run("worker")
    first = RunEvent(event_id="event-1", run_id="run-fake", stage="test", message="one")
    second = RunEvent(event_id="event-2", run_id="run-fake", stage="test", message="two")
    await crm.append_events("run-fake", claim.lease_token, [first], "same-key")
    with pytest.raises(IdempotencyConflict):
        await crm.append_events("run-fake", claim.lease_token, [second], "same-key")


@pytest.mark.asyncio
async def test_expired_lease_cannot_heartbeat_or_write() -> None:
    crm = FakeCRMPort()
    await crm.enqueue(snapshot())
    claim = await crm.claim_run("worker")
    run = await crm.inspect_run("run-fake")
    run.lease_expires_at = claim.lease_expires_at - timedelta(days=1)
    assert not await crm.heartbeat("run-fake", claim.lease_token)


@pytest.mark.asyncio
async def test_same_business_outbox_replay_accepts_a_new_lease() -> None:
    crm = FakeCRMPort()
    await crm.enqueue(snapshot())
    first_claim = await crm.claim_run("worker")
    candidate = ProspectCandidate(
        candidate_id="prospect-1",
        name="Clima Uno",
        location=ProspectLocation(comuna_code="13101", comuna_name="Santiago"),
    )
    await crm.upsert_candidates("run-fake", first_claim.lease_token, [candidate], "business-key-1")
    run = await crm.inspect_run("run-fake")
    run.lease_expires_at = first_claim.lease_expires_at - timedelta(days=1)
    second_claim = await crm.claim_run("worker")
    assert second_claim.lease_token != first_claim.lease_token

    await crm.upsert_candidates("run-fake", second_claim.lease_token, [candidate], "business-key-1")

    assert len(run.candidates) == 1
