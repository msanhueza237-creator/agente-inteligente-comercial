from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import product

from app.prospecting.contracts import (
    CandidateBatchAck,
    ClaimedRun,
    ClaimedTask,
    CompletionReport,
    ProspectCandidate,
    ProspectingRunSnapshot,
    RunEvent,
    RunStatus,
    SourceName,
)
from app.crm.port import CRMIdempotencyConflict, CRMLeaseLostError, HeartbeatResult


@dataclass
class _FakeRun:
    snapshot: ProspectingRunSnapshot
    tasks: list[ClaimedTask] = field(default_factory=list)
    status: RunStatus = RunStatus.pending
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    cancel_requested: bool = False
    events: list[RunEvent] = field(default_factory=list)
    candidates: dict[str, ProspectCandidate] = field(default_factory=dict)
    report: CompletionReport | None = None
    error: str | None = None


def _claimed_tasks(snapshot: ProspectingRunSnapshot) -> tuple[ClaimedTask, ...]:
    discovery_sources = tuple(
        source
        for source in snapshot.campaign.sources
        if source in {SourceName.google_places, SourceName.brave_search}
    )
    tasks: list[ClaimedTask] = []
    for source, keyword, territory in product(
        discovery_sources, snapshot.campaign.keywords, snapshot.campaign.territories
    ):
        scope = f"{snapshot.crm_run_id}:{source.value}:{keyword}:{territory.comuna_code}"
        tasks.append(
            ClaimedTask(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, scope)),
                source=source,
                keyword=keyword,
                region_code=territory.region_code,
                region_name=territory.region_name,
                comuna_code=territory.comuna_code,
                comuna_name=territory.comuna_name,
                max_results=snapshot.campaign.max_results_per_task,
            )
        )
    return tuple(tasks)


class IdempotencyConflict(CRMIdempotencyConflict):
    pass


class LeaseRejected(CRMLeaseLostError):
    pass


class FakeCRMPort:
    """Thread-safe development CRM with lease and idempotency semantics."""

    def __init__(self) -> None:
        self._runs: dict[str, _FakeRun] = {}
        self._idempotency: dict[str, str] = {}
        self._idempotency_results: dict[str, CandidateBatchAck] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, snapshot: ProspectingRunSnapshot) -> None:
        async with self._lock:
            self._runs.setdefault(
                snapshot.crm_run_id,
                _FakeRun(snapshot=snapshot, tasks=list(_claimed_tasks(snapshot))),
            )

    async def request_cancel(self, run_id: str) -> None:
        async with self._lock:
            self._runs[run_id].cancel_requested = True
            self._runs[run_id].status = RunStatus.cancel_requested
            self._runs[run_id].tasks = [
                task.model_copy(update={"status": "cancelled"})
                if task.status in {"pending", "running"}
                else task
                for task in self._runs[run_id].tasks
            ]

    async def claim_run(self, worker_id: str, lease_seconds: int = 120) -> ClaimedRun | None:
        del worker_id
        now = datetime.now(timezone.utc)
        async with self._lock:
            for run in self._runs.values():
                claimable = run.status == RunStatus.pending or (
                    run.status == RunStatus.running
                    and run.lease_expires_at is not None
                    and run.lease_expires_at <= now
                )
                if not claimable:
                    continue
                run.status = RunStatus.running
                run.lease_token = str(uuid.uuid4())
                run.lease_expires_at = now + timedelta(seconds=lease_seconds)
                return ClaimedRun(
                    snapshot=run.snapshot,
                    lease_token=run.lease_token,
                    lease_expires_at=run.lease_expires_at,
                    candidates_found=len(run.candidates),
                    tasks=tuple(run.tasks),
                )
        return None

    async def heartbeat(
        self, run_id: str, lease_token: str, lease_seconds: int = 120
    ) -> HeartbeatResult:
        now = datetime.now(timezone.utc)
        async with self._lock:
            run = self._runs.get(run_id)
            if (
                run is None
                or run.lease_token != lease_token
                or run.lease_expires_at is None
                or run.lease_expires_at <= now
            ):
                return HeartbeatResult(lease_valid=False)
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return HeartbeatResult(
                lease_valid=True,
                cancel_requested=run.cancel_requested,
            )

    async def is_cancel_requested(self, run_id: str, lease_token: str) -> bool:
        async with self._lock:
            run = self._runs[run_id]
            return run.lease_token != lease_token or run.cancel_requested

    @staticmethod
    def _payload_hash(payload: object) -> str:
        encoded = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _accept_idempotency(self, key: str, payload: object) -> bool:
        payload_hash = self._payload_hash(payload)
        previous = self._idempotency.get(key)
        if previous is None:
            self._idempotency[key] = payload_hash
            return True
        if previous != payload_hash:
            raise IdempotencyConflict(f"idempotency key {key!r} reused with different payload")
        return False

    def _require_lease(self, run_id: str, lease_token: str) -> _FakeRun:
        run = self._runs[run_id]
        now = datetime.now(timezone.utc)
        if (
            run.lease_token != lease_token
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            raise LeaseRejected("invalid or expired run lease")
        return run

    async def append_events(
        self, run_id: str, lease_token: str, events: list[RunEvent], idempotency_key: str
    ) -> None:
        async with self._lock:
            run = self._require_lease(run_id, lease_token)
            if self._accept_idempotency(idempotency_key, [e.model_dump() for e in events]):
                run.events.extend(events)
                for event in events:
                    if not event.task_id:
                        continue
                    for index, task in enumerate(run.tasks):
                        if task.task_id != event.task_id:
                            continue
                        attempt = event.metrics.get("attempt", task.attempts)
                        found = event.metrics.get(
                            "results_accepted", event.metrics.get("candidates_found", 0)
                        )
                        discarded = event.metrics.get(
                            "results_discarded", task.results_discarded
                        )
                        run.tasks[index] = task.model_copy(
                            update={
                                "status": event.task_status or task.status,
                                "attempts": max(task.attempts, int(attempt or 0)),
                                "candidates_found": max(
                                    task.candidates_found, int(found or 0)
                                ),
                                "results_discarded": max(
                                    task.results_discarded, int(discarded or 0)
                                ),
                            }
                        )
                        break

    async def upsert_candidates(
        self,
        run_id: str,
        lease_token: str,
        candidates: list[ProspectCandidate],
        idempotency_key: str,
    ) -> CandidateBatchAck:
        async with self._lock:
            self._require_lease(run_id, lease_token)
            if not self._accept_idempotency(idempotency_key, [c.model_dump() for c in candidates]):
                return self._idempotency_results[idempotency_key].model_copy(deep=True)
            run = self._runs[run_id]
            accepted_ids: list[str] = []
            rejected_limit = 0
            for candidate in candidates:
                key = candidate.candidate_id or self._payload_hash(candidate.model_dump())
                if key in run.candidates:
                    run.candidates[key] = candidate
                    continue
                if len(run.candidates) >= run.snapshot.campaign.max_candidates:
                    rejected_limit += 1
                    continue
                run.candidates[key] = candidate
                accepted_ids.append(key)
            result = CandidateBatchAck(
                accepted=len(accepted_ids),
                rejected_limit=rejected_limit,
                candidates_found=len(run.candidates),
                accepted_candidate_ids=tuple(accepted_ids),
            )
            self._idempotency_results[idempotency_key] = result
            return result.model_copy(deep=True)

    async def complete_run(
        self,
        run_id: str,
        lease_token: str,
        report: CompletionReport,
        idempotency_key: str,
        *,
        worker_id: str | None = None,
    ) -> None:
        del worker_id
        async with self._lock:
            payload = report.model_dump()
            if idempotency_key in self._idempotency:
                self._accept_idempotency(idempotency_key, payload)
                return
            self._require_lease(run_id, lease_token)
            if self._accept_idempotency(idempotency_key, payload):
                run = self._runs[run_id]
                run.status = RunStatus(report.status)
                run.report = report
                run.lease_token = None
                run.lease_expires_at = None

    async def fail_run(
        self,
        run_id: str,
        lease_token: str,
        error: str,
        idempotency_key: str,
        *,
        worker_id: str | None = None,
    ) -> None:
        del worker_id
        async with self._lock:
            payload = {"error": error}
            if idempotency_key in self._idempotency:
                self._accept_idempotency(idempotency_key, payload)
                return
            self._require_lease(run_id, lease_token)
            if self._accept_idempotency(idempotency_key, payload):
                run = self._runs[run_id]
                run.status = RunStatus.failed
                run.error = error
                run.lease_token = None
                run.lease_expires_at = None

    async def inspect_run(self, run_id: str) -> _FakeRun:
        async with self._lock:
            return self._runs[run_id]
