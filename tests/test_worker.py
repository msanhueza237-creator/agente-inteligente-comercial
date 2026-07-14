import pytest
import asyncio
from datetime import datetime, timedelta, timezone

from app.crm.fake import FakeCRMPort
from app.crm.port import CRMPermanentError, CRMRetryableError, HeartbeatResult
from app.prospecting.contracts import (
    CandidateBatchAck,
    ClaimedRun,
    ProspectCandidate,
    ProspectLocation,
    ProspectingCampaign,
    ProspectingRunSnapshot,
    RunEvent,
    RunStatus,
    SourceEvidence,
    SourceName,
    Territory,
)
from app.prospecting.sources import StaticSourceExecutor
from app.prospecting.store import MemoryWorkerStore
from app.prospecting.worker import ProspectingWorker, WorkerConfig


def build_snapshot(
    *,
    sources=(
        SourceName.google_places,
        SourceName.brave_search,
        SourceName.official_website,
    ),
    max_candidates=1000,
):
    return ProspectingRunSnapshot(
        crm_run_id="run-worker",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign-worker",
            name="Worker",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13101",
                    comuna_name="Santiago",
                ),
            ),
            keywords=("climatización",),
            sources=sources,
            max_candidates=max_candidates,
        ),
    )


def qualified_candidate(
    *,
    candidate_id: str | None = None,
    name: str = "Climatización Santiago SpA",
    url: str = "https://clima-santiago.cl",
    provider_id: str = "place-worker",
) -> ProspectCandidate:
    return ProspectCandidate(
        candidate_id=candidate_id,
        name=name,
        provider_ids={"google_places": provider_id},
        website=url,
        description="Instalación y mantención de aire acondicionado",
        location=ProspectLocation(
            region_code="13",
            region_name="Región Metropolitana de Santiago",
            comuna_code="13101",
            comuna_name="Santiago",
        ),
        evidence=[
            SourceEvidence(
                provider=SourceName.google_places,
                provider_record_id=provider_id,
                field="name",
                value=name,
            ),
            SourceEvidence(
                provider=SourceName.google_places,
                provider_record_id=provider_id,
                field="description",
                value="Instalación y mantención de aire acondicionado",
            ),
            SourceEvidence(
                provider=SourceName.google_places,
                provider_record_id=provider_id,
                field="location.region_code",
                value="13",
            ),
            SourceEvidence(
                provider=SourceName.google_places,
                provider_record_id=provider_id,
                field="location.region_name",
                value="Región Metropolitana de Santiago",
            ),
            SourceEvidence(
                provider=SourceName.google_places,
                provider_record_id=provider_id,
                field="location.comuna_code",
                value="13101",
            ),
            SourceEvidence(
                provider=SourceName.google_places,
                provider_record_id=provider_id,
                field="location.comuna_name",
                value="Santiago",
            ),
            SourceEvidence(
                provider=SourceName.google_places,
                provider_record_id=provider_id,
                field="website",
                value=url,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_worker_executes_tasks_deduplicates_and_completes() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    source = StaticSourceExecutor([qualified_candidate()])
    await crm.enqueue(build_snapshot())
    worker = ProspectingWorker(
        crm,
        store,
        source,
        worker_id="worker-test",
        config=WorkerConfig(lease_seconds=120, heartbeat_seconds=60, task_max_attempts=3),
    )

    assert await worker.poll_once()

    remote = await crm.inspect_run("run-worker")
    assert remote.status == RunStatus.completed
    assert len(remote.candidates) == 1
    saved_candidate = next(iter(remote.candidates.values()))
    assert saved_candidate.category == "tecnico"
    assert saved_candidate.score is not None and 0 <= saved_candidate.score <= 100
    assert len(source.calls) == 2
    local = store.runs["run-worker"]
    assert local.status == "completed"
    assert all(task.status == "completed" for task in local.tasks.values())


class FlakySource:
    def __init__(self, fail_count: int):
        self.fail_count = fail_count
        self.calls = 0

    async def search(self, task, snapshot):
        del task, snapshot
        self.calls += 1
        if self.calls <= self.fail_count:
            raise RuntimeError("temporary token=secret-value")
        return [qualified_candidate()]


class BlockingSource:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def search(self, task, snapshot):
        del task, snapshot
        self.started.set()
        await self.release.wait()
        return []


@pytest.mark.asyncio
async def test_task_started_event_reaches_crm_before_slow_source_finishes() -> None:
    crm = FakeCRMPort()
    source = BlockingSource()
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    worker = ProspectingWorker(
        crm,
        MemoryWorkerStore(),
        source,
        config=WorkerConfig(heartbeat_seconds=60),
    )

    polling = asyncio.create_task(worker.poll_once())
    await asyncio.wait_for(source.started.wait(), timeout=1)

    remote = await crm.inspect_run("run-worker")
    assert any(event.stage == "task_started" for event in remote.events)
    assert remote.tasks[0].status == "running"

    source.release.set()
    await polling


@pytest.mark.asyncio
async def test_worker_retries_persistently_then_succeeds() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    source = FlakySource(fail_count=2)
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    worker = ProspectingWorker(
        crm,
        store,
        source,
        worker_id="worker-retry",
        config=WorkerConfig(heartbeat_seconds=60, task_max_attempts=3),
    )

    await worker.poll_once()

    assert source.calls == 3
    assert (await crm.inspect_run("run-worker")).status == RunStatus.completed
    assert "secret-value" not in str((await crm.inspect_run("run-worker")).events)


@pytest.mark.asyncio
async def test_worker_marks_run_partial_when_all_configured_sources_fail() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    source = FlakySource(fail_count=99)
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    worker = ProspectingWorker(
        crm,
        store,
        source,
        config=WorkerConfig(heartbeat_seconds=60, task_max_attempts=3),
    )

    await worker.poll_once()

    assert source.calls == 3
    assert (await crm.inspect_run("run-worker")).status == RunStatus.partial


@pytest.mark.asyncio
async def test_candidate_cap_completes_and_reports_limit_reached() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    await crm.enqueue(build_snapshot(max_candidates=1))
    worker = ProspectingWorker(
        crm,
        store,
        StaticSourceExecutor([qualified_candidate()]),
        config=WorkerConfig(heartbeat_seconds=60),
    )

    await worker.poll_once()

    remote = await crm.inspect_run("run-worker")
    assert remote.status == RunStatus.completed
    assert remote.report is not None
    assert remote.report.stats["limit_reached"] is True
    assert {task.status for task in store.runs["run-worker"].tasks.values()} == {
        "completed",
        "cancelled",
    }


@pytest.mark.asyncio
async def test_cap_uses_crm_ack_so_remote_duplicate_does_not_hide_new_candidate() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    await crm.enqueue(
        build_snapshot(sources=(SourceName.google_places,), max_candidates=1000)
    )
    remote = await crm.inspect_run("run-worker")
    duplicate = qualified_candidate(
        candidate_id="remote-duplicate",
        provider_id="place-duplicate",
        url="https://clima-duplicada.cl",
        name="Climatización Duplicada SpA",
    )
    remote.candidates["remote-duplicate"] = duplicate
    for index in range(998):
        candidate_id = f"remote-{index}"
        remote.candidates[candidate_id] = ProspectCandidate(
            candidate_id=candidate_id,
            name=f"Prospecto remoto {index}",
            location=ProspectLocation(region_code="13", comuna_code="13101"),
        )
    new_candidate = qualified_candidate(
        candidate_id="remote-new",
        provider_id="place-new",
        url="https://clima-nueva.cl",
        name="Refrigeración Nueva SpA",
    )
    source = StaticSourceExecutor([duplicate, new_candidate])
    worker = ProspectingWorker(
        crm,
        store,
        source,
        config=WorkerConfig(heartbeat_seconds=60),
    )

    await worker.poll_once()

    assert len(remote.candidates) == 1000
    assert "remote-new" in remote.candidates
    assert remote.status == RunStatus.completed
    assert remote.report is not None
    assert remote.report.stats["candidates"] == 1000
    assert remote.report.stats["limit_reached"] is True


class CancellingSource:
    def __init__(self, crm):
        self.crm = crm

    async def search(self, task, snapshot):
        del task
        await self.crm.request_cancel(snapshot.crm_run_id)
        return [qualified_candidate()]


@pytest.mark.asyncio
async def test_worker_honours_cancellation_and_keeps_completed_results() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    await crm.enqueue(build_snapshot())
    worker = ProspectingWorker(
        crm,
        store,
        CancellingSource(crm),
        config=WorkerConfig(heartbeat_seconds=60),
    )

    await worker.poll_once()

    remote = await crm.inspect_run("run-worker")
    assert remote.status == RunStatus.cancelled
    assert len(remote.candidates) == 1
    statuses = {task.status for task in store.runs["run-worker"].tasks.values()}
    assert statuses == {"completed", "cancelled"}


class CancelOnHeartbeatCRM(FakeCRMPort):
    def __init__(self):
        super().__init__()
        self.cancelled_once = False

    async def heartbeat(self, run_id, lease_token, lease_seconds=120):
        ok = await super().heartbeat(run_id, lease_token, lease_seconds)
        if ok and not self.cancelled_once:
            self.cancelled_once = True
            await self.request_cancel(run_id)
        return ok


@pytest.mark.asyncio
async def test_cancellation_race_does_not_start_a_new_query() -> None:
    crm = CancelOnHeartbeatCRM()
    source = StaticSourceExecutor([qualified_candidate()])
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    worker = ProspectingWorker(
        crm,
        MemoryWorkerStore(),
        source,
        config=WorkerConfig(heartbeat_seconds=60),
    )

    await worker.poll_once()

    assert source.calls == []
    assert (await crm.inspect_run("run-worker")).status == RunStatus.cancelled


class SequentialQuotaSource:
    def __init__(self):
        self.started = asyncio.Event()
        self.external_calls = 0

    async def search(self, task, snapshot):
        del task, snapshot
        for _ in range(20):
            self.external_calls += 1
            self.started.set()
            await asyncio.sleep(0.05)
        return [qualified_candidate()]


class CancelDuringSearchCRM(FakeCRMPort):
    def __init__(self, source: SequentialQuotaSource):
        super().__init__()
        self.source = source
        self.cancel_signal_sent = False

    async def heartbeat(self, run_id, lease_token, lease_seconds=120):
        result = await super().heartbeat(run_id, lease_token, lease_seconds)
        if result and self.source.started.is_set() and not self.cancel_signal_sent:
            self.cancel_signal_sent = True
            await self.request_cancel(run_id)
            return HeartbeatResult(lease_valid=True, cancel_requested=True)
        return result


@pytest.mark.asyncio
async def test_heartbeat_cancellation_stops_new_source_calls() -> None:
    source = SequentialQuotaSource()
    crm = CancelDuringSearchCRM(source)
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    worker = ProspectingWorker(
        crm,
        MemoryWorkerStore(),
        source,
        config=WorkerConfig(heartbeat_seconds=0.005),
    )

    await worker.poll_once()

    remote = await crm.inspect_run("run-worker")
    assert crm.cancel_signal_sent
    assert remote.status == RunStatus.cancelled
    assert source.external_calls == 1
    assert remote.candidates == {}


class TransientCandidateCRM(FakeCRMPort):
    def __init__(self):
        super().__init__()
        self.fail_once = True

    async def upsert_candidates(self, run_id, lease_token, candidates, idempotency_key):
        if self.fail_once:
            self.fail_once = False
            raise CRMRetryableError("CRM returned status 425")
        return await super().upsert_candidates(
            run_id, lease_token, candidates, idempotency_key
        )


class TransientTaskCompletedCRM(FakeCRMPort):
    def __init__(self):
        super().__init__()
        self.fail_completed_once = True

    async def append_events(self, run_id, lease_token, events, idempotency_key):
        if self.fail_completed_once and any(
            event.stage == "task_completed" for event in events
        ):
            self.fail_completed_once = False
            raise CRMRetryableError("CRM returned status 500")
        return await super().append_events(
            run_id, lease_token, events, idempotency_key
        )


class CommitThenLoseCompleteResponseCRM(FakeCRMPort):
    def __init__(self):
        super().__init__()
        self.lose_response_once = True
        self.replay_worker_ids: list[str | None] = []

    async def complete_run(
        self,
        run_id,
        lease_token,
        report,
        idempotency_key,
        *,
        worker_id=None,
    ):
        self.replay_worker_ids.append(worker_id)
        await super().complete_run(
            run_id,
            lease_token,
            report,
            idempotency_key,
            worker_id=worker_id,
        )
        if self.lose_response_once:
            self.lose_response_once = False
            raise CRMRetryableError("CRM transport failed after commit")


@pytest.mark.asyncio
async def test_outbox_recovers_without_repeating_completed_source_query() -> None:
    crm = TransientCandidateCRM()
    store = MemoryWorkerStore()
    source = StaticSourceExecutor([qualified_candidate()])
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    worker = ProspectingWorker(
        crm,
        store,
        source,
        config=WorkerConfig(heartbeat_seconds=60),
    )

    await worker.poll_once()
    assert (await crm.inspect_run("run-worker")).status == RunStatus.running
    assert len(source.calls) == 1
    assert (await store.summary("run-worker")).dead_letters == 0
    assert any(
        message.kind == "candidates"
        for key, message in store.runs["run-worker"].outbox.items()
        if key not in store.runs["run-worker"].outbox_dead
        and key not in store.runs["run-worker"].outbox_delivered
    )

    remote = await crm.inspect_run("run-worker")
    remote.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    for message_id in store.runs["run-worker"].outbox_available_at:
        store.runs["run-worker"].outbox_available_at[message_id] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    await worker.poll_once()

    assert remote.status == RunStatus.completed
    assert len(source.calls) == 1
    assert len(remote.candidates) == 1


@pytest.mark.asyncio
async def test_pending_task_completed_event_prevents_source_reexecution() -> None:
    crm = TransientTaskCompletedCRM()
    store = MemoryWorkerStore()
    source = StaticSourceExecutor([qualified_candidate()])
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    worker = ProspectingWorker(
        crm,
        store,
        source,
        config=WorkerConfig(heartbeat_seconds=60),
    )

    await worker.poll_once()

    remote = await crm.inspect_run("run-worker")
    assert remote.status == RunStatus.running
    assert len(remote.candidates) == 1
    assert len(source.calls) == 1
    local = store.runs["run-worker"]
    assert {task.status for task in local.tasks.values()} == {"completed"}
    pending_completion = next(
        message
        for key, message in local.outbox.items()
        if key not in local.outbox_delivered
        and any(event["stage"] == "task_completed" for event in message.payload)
    )
    task_id = next(iter(local.tasks))
    assert task_id not in pending_completion.idempotency_key

    remote.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    for message_id in local.outbox_available_at:
        local.outbox_available_at[message_id] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    await worker.poll_once()

    assert remote.status == RunStatus.completed
    assert len(source.calls) == 1
    assert remote.tasks[0].status == "completed"
    assert remote.tasks[0].attempts == 1


@pytest.mark.asyncio
async def test_terminal_outbox_replays_after_commit_without_new_claim() -> None:
    crm = CommitThenLoseCompleteResponseCRM()
    store = MemoryWorkerStore()
    source = StaticSourceExecutor([qualified_candidate()])
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    first_worker = ProspectingWorker(
        crm,
        store,
        source,
        worker_id="worker-original",
        config=WorkerConfig(heartbeat_seconds=60),
    )

    await first_worker.poll_once()

    remote = await crm.inspect_run("run-worker")
    local = store.runs["run-worker"]
    assert remote.status == RunStatus.completed
    assert local.status == "running"
    assert len(source.calls) == 1
    assert await store.has_pending_outbox("run-worker")

    restarted_worker = ProspectingWorker(
        crm,
        store,
        source,
        worker_id="worker-after-restart",
        config=WorkerConfig(heartbeat_seconds=60),
    )
    assert await restarted_worker.poll_once()

    assert local.status == "completed"
    assert not await store.has_pending_outbox("run-worker")
    assert len(source.calls) == 1
    assert crm.replay_worker_ids == [None, "worker-original"]


class SelectiveFailureSource:
    async def search(self, task, snapshot):
        del snapshot
        if task.source == SourceName.google_places:
            raise RuntimeError("Google temporarily unavailable")
        return [qualified_candidate()]


@pytest.mark.asyncio
async def test_one_failed_source_and_one_successful_source_finishes_partial() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    await crm.enqueue(build_snapshot(max_candidates=1))
    worker = ProspectingWorker(
        crm,
        store,
        SelectiveFailureSource(),
        config=WorkerConfig(heartbeat_seconds=60, task_max_attempts=2),
    )

    await worker.poll_once()

    remote = await crm.inspect_run("run-worker")
    assert remote.status == RunStatus.partial
    assert len(remote.candidates) == 1
    assert remote.report is not None
    assert remote.report.stats["limit_reached"] is True


class RaisingHeartbeatCRM(FakeCRMPort):
    async def heartbeat(self, run_id, lease_token, lease_seconds=120):
        del run_id, lease_token, lease_seconds
        raise ConnectionError("CRM unavailable")


@pytest.mark.asyncio
async def test_background_heartbeat_exception_marks_lease_lost() -> None:
    crm = RaisingHeartbeatCRM()
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    claim = await crm.claim_run("worker-heartbeat")
    worker = ProspectingWorker(
        crm,
        MemoryWorkerStore(),
        StaticSourceExecutor([qualified_candidate()]),
        config=WorkerConfig(heartbeat_seconds=0.001),
    )
    lost = asyncio.Event()

    heartbeat = asyncio.create_task(worker._run_heartbeat(claim, lost))
    await asyncio.wait_for(lost.wait(), timeout=0.2)

    assert lost.is_set()
    await heartbeat


class AlwaysClaimCRM:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.claim_count = 0

    async def claim_run(self, worker_id, lease_seconds=120):
        del worker_id, lease_seconds
        self.claim_count += 1
        return ClaimedRun(
            snapshot=self.snapshot,
            lease_token="6ca39316-2608-483b-bb64-87c1a704f145",
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            tasks=(),
        )

    async def heartbeat(self, run_id, lease_token, lease_seconds=120):
        del run_id, lease_token, lease_seconds
        return True

    async def is_cancel_requested(self, run_id, lease_token):
        del run_id, lease_token
        return False

    async def append_events(self, run_id, lease_token, events, idempotency_key):
        del run_id, lease_token, events, idempotency_key

    async def upsert_candidates(self, run_id, lease_token, candidates, idempotency_key):
        del run_id, lease_token, candidates, idempotency_key
        return CandidateBatchAck()

    async def complete_run(self, run_id, lease_token, report, idempotency_key):
        del run_id, lease_token, report, idempotency_key

    async def fail_run(self, run_id, lease_token, error, idempotency_key):
        del run_id, lease_token, error, idempotency_key


@pytest.mark.asyncio
async def test_run_forever_respects_poll_cadence_even_after_a_claim() -> None:
    crm = AlwaysClaimCRM(build_snapshot(sources=(SourceName.google_places,)))
    worker = ProspectingWorker(
        crm,
        MemoryWorkerStore(),
        StaticSourceExecutor(),
        config=WorkerConfig(poll_seconds=0.02, heartbeat_seconds=60),
    )

    running = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.075)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)

    assert 2 <= crm.claim_count <= 6


class PermanentCandidateCRM(FakeCRMPort):
    async def upsert_candidates(self, run_id, lease_token, candidates, idempotency_key):
        del run_id, lease_token, candidates, idempotency_key
        raise CRMPermanentError("CRM returned status 400")


@pytest.mark.asyncio
async def test_permanent_candidate_delivery_failure_dead_letters_and_never_completes() -> None:
    crm = PermanentCandidateCRM()
    store = MemoryWorkerStore()
    source = StaticSourceExecutor([qualified_candidate()])
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,), max_candidates=1))
    worker = ProspectingWorker(
        crm,
        store,
        source,
        config=WorkerConfig(heartbeat_seconds=60),
    )

    await worker.poll_once()
    remote = await crm.inspect_run("run-worker")
    assert remote.status == RunStatus.running
    assert len(remote.candidates) == 0

    remote.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await worker.poll_once()

    assert remote.status == RunStatus.partial
    assert remote.status != RunStatus.completed
    assert len(remote.candidates) == 0
    assert len(source.calls) == 1
    assert remote.report is not None
    assert remote.report.stats["dead_letters"] == 1
    assert remote.report.stats["delivery_failed"] is True
    assert "limit_reached" not in remote.report.stats


@pytest.mark.asyncio
async def test_retryable_outbox_message_survives_more_than_max_attempts_and_recovers() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    claim = await crm.claim_run("worker-outbox")
    run_id = await store.ensure_run(claim, task_max_attempts=3)
    event = RunEvent(
        event_id="e0e1a944-af3e-4e0e-a954-379250168a17",
        run_id=claim.snapshot.crm_run_id,
        stage="outbox_retry_test",
        message="retry",
    )
    await store.save_event(run_id, None, event)
    message = (await store.get_outbox(run_id))[0]

    for _ in range(15):
        assert not await store.mark_outbox_failed(
            message.id, "CRM returned status 500", retryable=True
        )

    assert (await store.summary(run_id)).dead_letters == 0
    assert await store.has_pending_outbox(run_id)
    store.runs[run_id].outbox_available_at[message.id] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    assert (await store.get_outbox(run_id))[0].id == message.id
    await store.mark_outbox_delivered(message.id)
    assert not await store.has_pending_outbox(run_id)


@pytest.mark.asyncio
async def test_global_dedup_never_leaks_evidence_from_another_run() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    campaign_a = build_snapshot(
        sources=(SourceName.brave_search, SourceName.official_website)
    ).campaign.model_copy(update={"crm_campaign_id": "campaign-a"})
    campaign_b = build_snapshot(
        sources=(SourceName.google_places,)
    ).campaign.model_copy(update={"crm_campaign_id": "campaign-b"})
    snapshot_a = build_snapshot().model_copy(
        update={"crm_run_id": "run-a", "campaign": campaign_a}
    )
    snapshot_b = build_snapshot().model_copy(
        update={"crm_run_id": "run-b", "campaign": campaign_b}
    )
    await crm.enqueue(snapshot_a)
    await crm.enqueue(snapshot_b)
    claim_a = await crm.claim_run("worker-a")
    run_a = await store.ensure_run(claim_a, 3)
    task_a = await store.claim_task(run_a, "worker-a", 120)
    google_candidate = qualified_candidate(provider_id="google-run-b")
    brave_evidence = [
        SourceEvidence(
            provider=SourceName.brave_search,
            provider_record_id="brave-run-a",
            source_url="https://clima-santiago.cl",
            field=item.field,
            value=item.value,
        )
        for item in google_candidate.evidence
    ]
    brave_candidate = google_candidate.model_copy(
        update={
            "provider_ids": {"brave_search": "brave-run-a"},
            "evidence": brave_evidence,
        }
    )
    saved_a = await store.save_candidates(run_a, task_a, [brave_candidate])

    claim_b = await crm.claim_run("worker-b")
    run_b = await store.ensure_run(claim_b, 3)
    task_b = await store.claim_task(run_b, "worker-b", 120)
    saved_b = await store.save_candidates(run_b, task_b, [google_candidate])

    assert saved_a[0].candidate_id == saved_b[0].candidate_id
    assert saved_a[0].import_eligible
    assert not saved_b[0].import_eligible
    assert saved_b[0].provider_ids == {"google_places": "google-run-b"}
    assert {item.provider for item in saved_b[0].evidence} == {
        SourceName.google_places
    }


@pytest.mark.asyncio
async def test_ambiguous_exact_identifiers_create_one_stable_review_row() -> None:
    crm = FakeCRMPort()
    store = MemoryWorkerStore()
    await crm.enqueue(build_snapshot(sources=(SourceName.google_places,)))
    claim = await crm.claim_run("worker-ambiguous")
    run_id = await store.ensure_run(claim, 3)
    task = await store.claim_task(run_id, "worker-ambiguous", 120)
    entity_a = qualified_candidate(
        candidate_id="entity-a",
        name="Clima RUT SpA",
        url="https://entity-a.cl",
        provider_id="place-a",
    ).model_copy(update={"rut": "12.345.678-5"})
    entity_b = qualified_candidate(
        candidate_id="entity-b",
        name="Refrigeración Dominio SpA",
        url="https://shared-conflict.cl",
        provider_id="place-b",
    )
    await store.save_candidates(run_id, task, [entity_a, entity_b])
    conflict = qualified_candidate(
        name="HVAC Conflicto SpA",
        url="https://shared-conflict.cl",
        provider_id="place-conflict",
    ).model_copy(update={"rut": "12.345.678-5"})

    first = await store.save_candidates(run_id, task, [conflict])
    second = await store.save_candidates(run_id, task, [conflict])

    rows = store.runs[run_id].candidates
    assert len(rows) == 3
    assert rows["entity-a"].name == "Clima RUT SpA"
    assert rows["entity-b"].name == "Refrigeración Dominio SpA"
    assert first[0].candidate_id == second[0].candidate_id
    assert first[0].candidate_id.startswith("prospect_review_")
    assert first[0].dedup_disposition.value == "possible_duplicate"
