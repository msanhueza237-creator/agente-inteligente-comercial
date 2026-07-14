from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.prospecting.contracts import (
    CandidateBatchAck,
    ClaimedRun,
    CompletionReport,
    ProspectCandidate,
    RunEvent,
)


class CRMPortError(RuntimeError):
    pass


class CRMRetryableError(CRMPortError):
    pass


class CRMLeaseLostError(CRMPortError):
    pass


class CRMIdempotencyConflict(CRMPortError):
    pass


class CRMPermanentError(CRMPortError):
    pass


@dataclass(frozen=True)
class HeartbeatResult:
    lease_valid: bool
    cancel_requested: bool = False

    def __bool__(self) -> bool:
        return self.lease_valid


class CRMPort(Protocol):
    """Only boundary the worker uses to communicate with the CRM.

    The development implementation is in-memory. A future HTTP adapter can
    implement this protocol without leaking an API key into the worker domain.
    """

    async def claim_run(self, worker_id: str, lease_seconds: int = 120) -> ClaimedRun | None: ...

    async def heartbeat(
        self, run_id: str, lease_token: str, lease_seconds: int = 120
    ) -> HeartbeatResult: ...

    async def is_cancel_requested(self, run_id: str, lease_token: str) -> bool: ...

    async def append_events(
        self, run_id: str, lease_token: str, events: list[RunEvent], idempotency_key: str
    ) -> None: ...

    async def upsert_candidates(
        self,
        run_id: str,
        lease_token: str,
        candidates: list[ProspectCandidate],
        idempotency_key: str,
    ) -> CandidateBatchAck: ...

    async def complete_run(
        self,
        run_id: str,
        lease_token: str,
        report: CompletionReport,
        idempotency_key: str,
        *,
        worker_id: str | None = None,
    ) -> None: ...

    async def fail_run(
        self,
        run_id: str,
        lease_token: str,
        error: str,
        idempotency_key: str,
        *,
        worker_id: str | None = None,
    ) -> None: ...
