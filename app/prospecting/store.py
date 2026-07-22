from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    CRMOutboxMessage,
    ProspectEvidenceRecord,
    ProspectingCandidateRecord,
    ProspectingEventRecord,
    ProspectingRun,
    ProspectingTask,
)
from app.normalization.address import normalize_address
from app.normalization.name import normalize_name
from app.normalization.phone import normalize_phone
from app.normalization.rut import normalize_rut
from app.normalization.website import normalize_website
from app.prospecting.contracts import (
    CandidateBatchAck,
    ClaimedRun,
    ClaimedTask,
    DedupDisposition,
    ProspectCandidate,
    ProspectingRunSnapshot,
    RunEvent,
    SourceEvidence,
    SourceName,
    Territory,
)
from app.prospecting.dedup import CandidateMatch, match_candidate, merge_exact_candidate
from app.prospecting.evidence import compact_evidence
from app.prospecting.validation import (
    has_compatible_evidence,
    location_is_in_requested_territory,
    normalize_evidence_value,
    normalize_geo,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TaskSpec:
    source: SourceName
    keyword: str
    region_code: str
    region_name: str
    comuna_code: str
    comuna_name: str
    max_results: int


def expand_task_specs(snapshot: ProspectingRunSnapshot) -> list[TaskSpec]:
    """Expand discovery work. official_website enriches hits; it is not a search source."""
    discovery_sources = tuple(
        source
        for source in snapshot.campaign.sources
        if source in {SourceName.google_places, SourceName.brave_search}
    )
    return [
        TaskSpec(
            source=source,
            keyword=keyword,
            region_code=territory.region_code,
            region_name=territory.region_name,
            comuna_code=territory.comuna_code,
            comuna_name=territory.comuna_name,
            max_results=snapshot.campaign.max_results_per_task,
        )
        for source in discovery_sources
        for keyword in snapshot.campaign.keywords
        for territory in snapshot.campaign.territories
    ]


def validate_claimed_task(snapshot: ProspectingRunSnapshot, task: ClaimedTask) -> Territory:
    discovery_sources = {SourceName.google_places, SourceName.brave_search}
    if task.source not in discovery_sources or task.source not in snapshot.campaign.sources:
        raise ValueError("claimed task source is outside the run snapshot")
    if task.keyword not in snapshot.campaign.keywords:
        raise ValueError("claimed task keyword is outside the run snapshot")
    if task.max_results > snapshot.campaign.max_results_per_task:
        raise ValueError("claimed task result limit exceeds the run snapshot")
    territory = next(
        (
            item
            for item in snapshot.campaign.territories
            if item.region_code == task.region_code and item.comuna_code == task.comuna_code
        ),
        None,
    )
    if territory is None:
        raise ValueError("claimed task is outside the run snapshot")
    return territory


@dataclass(frozen=True)
class WorkerTask:
    id: str
    run_id: str
    source: SourceName
    keyword: str
    region_code: str
    region_name: str
    comuna_code: str
    comuna_name: str
    max_results: int
    attempt_count: int
    max_attempts: int


@dataclass(frozen=True)
class OutboxEnvelope:
    id: str
    run_id: str
    kind: str
    idempotency_key: str
    payload: dict | list


@dataclass(frozen=True)
class TerminalOutboxReplay:
    local_run_id: str
    crm_run_id: str
    worker_id: str
    lease_token: str
    message: OutboxEnvelope


@dataclass(frozen=True)
class RunSummary:
    pending: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    candidates: int = 0
    dead_letters: int = 0
    crm_accepted: int = 0
    rejected_limit: int = 0
    rejected_invalid: int = 0
    budget_limited: int = 0

    @property
    def work_remaining(self) -> bool:
        return self.pending > 0 or self.running > 0


def _completed_task_ids_from_event_payloads(
    payloads: list[dict | list],
) -> set[str]:
    """Return tasks whose durable outbox payload reports terminal completion.

    Event idempotency keys intentionally identify the event, not its task.  The
    payload is therefore the authoritative durable association used while the
    CRM still exposes the pre-event task state after a delivery failure.
    """
    completed: set[str] = set()
    for payload in payloads:
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict) or event.get("task_status") != "completed":
                continue
            task_id = event.get("task_id")
            if task_id:
                completed.add(str(task_id))
    return completed


class WorkerStore(Protocol):
    async def ensure_run(
        self,
        claim: ClaimedRun,
        task_max_attempts: int,
        worker_id: str | None = None,
    ) -> str: ...

    async def claim_task(
        self, run_id: str, worker_id: str, lease_seconds: int
    ) -> WorkerTask | None: ...

    async def heartbeat_task(self, task_id: str, worker_id: str, lease_seconds: int) -> bool: ...

    async def finish_task(
        self, task_id: str, worker_id: str, *, results: int, rejected: int
    ) -> None: ...

    async def fail_task(self, task_id: str, worker_id: str, error: str) -> None: ...

    async def cancel_tasks(self, run_id: str) -> None: ...

    async def save_candidates(
        self, run_id: str, task: WorkerTask, candidates: list[ProspectCandidate]
    ) -> list[ProspectCandidate]: ...

    async def partition_discoveries(
        self, run_id: str, candidates: list[ProspectCandidate]
    ) -> tuple[list[ProspectCandidate], list[ProspectCandidate]]: ...

    async def save_event(self, run_id: str, task: WorkerTask | None, event: RunEvent) -> None: ...

    async def get_outbox(self, run_id: str, limit: int = 50) -> list[OutboxEnvelope]: ...

    async def has_pending_outbox(self, run_id: str) -> bool: ...

    async def pending_terminal_replays(self, limit: int = 50) -> list[TerminalOutboxReplay]: ...

    async def mark_outbox_delivered(self, message_id: str) -> None: ...

    async def finalize_terminal_outbox(
        self,
        run_id: str,
        message_id: str,
        status: str,
        *,
        stats: dict | None = None,
        error: str | None = None,
    ) -> None: ...

    async def mark_outbox_failed(self, message_id: str, error: str, *, retryable: bool) -> bool: ...

    async def split_candidate_outbox(self, message_id: str, error: str) -> bool: ...

    async def discard_candidate_outbox(self, message_id: str, error: str) -> bool: ...

    async def record_candidate_ack(
        self, run_id: str, message_id: str, ack: CandidateBatchAck
    ) -> None: ...

    async def queue_terminal(
        self, run_id: str, kind: str, payload: dict, idempotency_key: str
    ) -> None: ...

    async def summary(self, run_id: str) -> RunSummary: ...

    async def set_run_status(
        self, run_id: str, status: str, *, stats: dict | None = None, error: str | None = None
    ) -> None: ...


def candidate_fingerprint(candidate: ProspectCandidate) -> str:
    rut = normalize_rut(candidate.rut)
    if rut:
        identity = f"rut:{rut}"
    elif candidate.provider_ids:
        provider, value = sorted(candidate.provider_ids.items())[0]
        identity = f"provider:{provider}:{value}"
    elif domain := normalize_website(candidate.website):
        identity = f"domain:{domain}"
    elif phone := normalize_phone(candidate.phone):
        identity = f"phone:{phone}"
    else:
        name, _ = normalize_name(candidate.name)
        comuna = normalize_geo(candidate.location.comuna_code or candidate.location.comuna_name)
        address = normalize_address(candidate.location.address) or ""
        identity = f"name_comuna_address:{name}:{comuna}:{address}"
    return hashlib.sha256(identity.encode()).hexdigest()


def stable_candidate_id(candidate: ProspectCandidate) -> str:
    return f"prospect_{candidate_fingerprint(candidate)[:24]}"


def review_candidate_fingerprint(candidate: ProspectCandidate) -> str:
    """Identity for an ambiguous row that must never collide with an entity."""

    name, _ = normalize_name(candidate.name)
    payload = {
        "rut": normalize_rut(candidate.rut),
        "providers": sorted(candidate.provider_ids.items()),
        "domain": normalize_website(candidate.website),
        "phone": normalize_phone(candidate.phone),
        "name": name,
        "locations": sorted(
            (
                normalize_geo(location.region_code or location.region_name),
                normalize_geo(location.comuna_code or location.comuna_name),
                normalize_address(location.address) or "",
            )
            for location in candidate.locations
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"review:" + encoded).hexdigest()


def candidate_id_for_match(candidate: ProspectCandidate, match: CandidateMatch) -> str:
    if match.disposition == DedupDisposition.possible_duplicate:
        return f"prospect_review_{review_candidate_fingerprint(candidate)[:24]}"
    return candidate.candidate_id or stable_candidate_id(candidate)


_INDEXED_LOCATION_FIELD = re.compile(r"^locations\[(\d+)]\.(.+)$")


def index_candidate_location_evidence(candidate: ProspectCandidate) -> ProspectCandidate:
    """Add stable per-location evidence fields for the CRM location mapper."""

    evidence = list(candidate.evidence)
    legacy = [item for item in evidence if not _INDEXED_LOCATION_FIELD.fullmatch(item.field)]
    indexed: list[SourceEvidence] = [
        item for item in evidence if _INDEXED_LOCATION_FIELD.fullmatch(item.field)
    ]

    def source_key(item: SourceEvidence) -> tuple:
        return (
            item.provider.value,
            item.provider_record_id,
            item.source_url,
        )

    for index, location in enumerate(candidate.locations):
        anchor_sources: set[tuple] | None
        if len(candidate.locations) == 1:
            anchor_sources = None
        elif location.address:
            expected_address = normalize_evidence_value("location.address", location.address)
            anchor_sources = {
                source_key(item)
                for item in legacy
                if item.field == "location.address"
                and normalize_evidence_value("location.address", item.value) == expected_address
            }
            if not anchor_sources:
                continue
        else:
            # Multiple same-comuna locations cannot be attributed safely
            # without an address anchor or pre-indexed source evidence.
            continue
        for attribute in ("region_code", "region_name", "comuna_code", "comuna_name", "address"):
            value = getattr(location, attribute)
            if not value:
                continue
            legacy_field = f"location.{attribute}"
            expected = normalize_evidence_value(legacy_field, value)
            for item in legacy:
                if item.field != legacy_field:
                    continue
                if normalize_evidence_value(legacy_field, item.value) != expected:
                    continue
                if anchor_sources is not None and source_key(item) not in anchor_sources:
                    continue
                indexed.append(item.model_copy(update={"field": f"locations[{index}].{attribute}"}))
    return candidate.model_copy(update={"evidence": compact_evidence([*legacy, *indexed])})


def assess_import_eligibility(candidate: ProspectCandidate) -> ProspectCandidate:
    """Mark importable candidates.

    Official/Brave evidence remains the strongest signal. Google Places-only
    prospects are also importable when they have a usable phone/email and a
    canonical territory; they are reviewed as contact-only prospects in the CRM.
    """

    permanent = [item for item in candidate.evidence if item.retention_until is None]
    current_contact_sources = [
        item
        for item in candidate.evidence
        if item.retention_until is None or item.provider == SourceName.google_places
    ]
    name_ok = has_compatible_evidence(candidate, "name", candidate.name, evidence_items=permanent)
    contact_ok = any(
        value and has_compatible_evidence(candidate, field_name, value, evidence_items=permanent)
        for field_name, value in (
            ("phone", candidate.phone),
            ("email", candidate.email),
            ("website", candidate.website),
        )
    )
    contact_only_name_ok = has_compatible_evidence(
        candidate, "name", candidate.name, evidence_items=current_contact_sources
    )
    contact_only_contact_ok = any(
        value
        and has_compatible_evidence(candidate, field_name, value, evidence_items=current_contact_sources)
        for field_name, value in (("phone", candidate.phone), ("email", candidate.email))
    )
    importable: list[int] = []
    for index, location in enumerate(candidate.locations):
        required = (
            (f"locations[{index}].region_code", "location.region_code", location.region_code),
            (f"locations[{index}].comuna_code", "location.comuna_code", location.comuna_code),
        )
        if all(
            value
            and any(
                item.field == indexed_field
                and normalize_evidence_value(legacy_field, item.value)
                == normalize_evidence_value(legacy_field, value)
                for item in permanent
            )
            for indexed_field, legacy_field, value in required
        ):
            importable.append(index)

    if not importable and contact_only_name_ok and contact_only_contact_ok:
        for index, location in enumerate(candidate.locations):
            if location.region_code and location.comuna_code:
                importable.append(index)
                break

    eligible = bool((name_ok and contact_ok and importable) or (contact_only_name_ok and contact_only_contact_ok and importable))
    flags: list[str] = []
    if not eligible:
        flags.append("insufficient_permanent_evidence")
    elif not (name_ok and contact_ok):
        flags.append("contact_only_import")
    flags.extend(
        f"location_{index}_temporary_evidence"
        for index in range(len(candidate.locations))
        if index not in importable and "contact_only_import" not in flags
    )
    return candidate.model_copy(
        update={
            "import_eligible": eligible,
            "importable_location_indexes": tuple(importable),
            "review_flags": tuple(flags),
        }
    )


def scope_candidate_locations(
    candidate: ProspectCandidate, snapshot: ProspectingRunSnapshot
) -> ProspectCandidate:
    locations = [
        location
        for location in candidate.locations
        if location_is_in_requested_territory(location, snapshot)
    ]
    if not locations:
        raise ValueError("candidate has no location inside the run snapshot")
    canonical = (
        candidate.location
        if location_is_in_requested_territory(candidate.location, snapshot)
        else locations[0]
    )
    allowed_location_values = {
        "location.region_code": {
            location.region_code for location in locations if location.region_code
        },
        "location.region_name": {
            normalize_geo(location.region_name) for location in locations if location.region_name
        },
        "location.comuna_code": {
            location.comuna_code for location in locations if location.comuna_code
        },
        "location.comuna_name": {
            normalize_geo(location.comuna_name) for location in locations if location.comuna_name
        },
        "location.address": {
            normalize_address(location.address)
            for location in locations
            if normalize_address(location.address)
        },
    }

    def evidence_belongs_to_scoped_location(field: str, value: str) -> bool:
        allowed = allowed_location_values.get(field)
        if allowed is None:
            return True
        if field in {"location.region_name", "location.comuna_name"}:
            normalized = normalize_geo(value)
        elif field == "location.address":
            normalized = normalize_address(value)
        else:
            normalized = value.strip()
        return normalized in allowed

    new_index_by_location = {
        (
            normalize_geo(location.region_code or location.region_name),
            normalize_geo(location.comuna_code or location.comuna_name),
            normalize_address(location.address) or "",
        ): index
        for index, location in enumerate(locations)
    }
    evidence: list[SourceEvidence] = []
    for item in candidate.evidence:
        indexed_match = _INDEXED_LOCATION_FIELD.fullmatch(item.field)
        if indexed_match:
            old_index = int(indexed_match.group(1))
            if old_index >= len(candidate.locations):
                continue
            old_location = candidate.locations[old_index]
            old_key = (
                normalize_geo(old_location.region_code or old_location.region_name),
                normalize_geo(old_location.comuna_code or old_location.comuna_name),
                normalize_address(old_location.address) or "",
            )
            new_index = new_index_by_location.get(old_key)
            if new_index is not None:
                evidence.append(
                    item.model_copy(
                        update={"field": f"locations[{new_index}].{indexed_match.group(2)}"}
                    )
                )
            continue
        if not item.field.startswith("location.") or evidence_belongs_to_scoped_location(
            item.field, item.value
        ):
            evidence.append(item)
    prepared = candidate.model_copy(
        update={"location": canonical, "locations": locations, "evidence": evidence}
    )
    return assess_import_eligibility(index_candidate_location_evidence(prepared))


class SQLWorkerStore:
    """PostgreSQL implementation with row locks and recoverable task leases."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._sessions = session_factory

    async def ensure_run(
        self,
        claim: ClaimedRun,
        task_max_attempts: int,
        worker_id: str | None = None,
    ) -> str:
        snapshot = claim.snapshot
        async with self._sessions() as session, session.begin():
            run = (
                await session.execute(
                    select(ProspectingRun)
                    .where(ProspectingRun.crm_run_id == snapshot.crm_run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            is_new = run is None
            if is_new:
                run = ProspectingRun(
                    crm_run_id=snapshot.crm_run_id,
                    crm_campaign_id=snapshot.campaign.crm_campaign_id,
                    campaign_version=snapshot.campaign_version,
                    snapshot=snapshot.model_dump(mode="json"),
                    status="running",
                    started_at=now_utc(),
                )
                session.add(run)
                await session.flush()

            existing_tasks = {
                str(task.id): task
                for task in (
                    await session.execute(
                        select(ProspectingTask)
                        .where(ProspectingTask.run_id == run.id)
                        .with_for_update()
                    )
                ).scalars()
            }
            queued_completion_task_ids = _completed_task_ids_from_event_payloads(
                list(
                    (
                        await session.execute(
                            select(CRMOutboxMessage.payload).where(
                                CRMOutboxMessage.run_id == run.id,
                                CRMOutboxMessage.kind == "events",
                                CRMOutboxMessage.status == "queued",
                            )
                        )
                    ).scalars()
                )
            )
            if claim.tasks is not None:
                remote_ids: set[str] = set()
                for claimed_task in claim.tasks:
                    territory = validate_claimed_task(snapshot, claimed_task)
                    task_id = str(uuid.UUID(claimed_task.task_id))
                    remote_ids.add(task_id)
                    task = existing_tasks.get(task_id)
                    if task is None:
                        task = ProspectingTask(id=uuid.UUID(task_id), run_id=run.id)
                        session.add(task)
                    preserve_local_completion = False
                    if task.status == "completed" and claimed_task.status in {
                        "pending",
                        "running",
                    }:
                        preserve_local_completion = task_id in queued_completion_task_ids
                    task.source = claimed_task.source.value
                    task.keyword = claimed_task.keyword
                    task.region_code = claimed_task.region_code
                    task.region_name = claimed_task.region_name or territory.region_name
                    task.comuna_code = claimed_task.comuna_code
                    task.comuna_name = claimed_task.comuna_name or territory.comuna_name
                    task.max_results = claimed_task.max_results
                    task.max_attempts = claimed_task.max_attempts
                    task.results_count = claimed_task.candidates_found
                    task.rejected_count = claimed_task.results_discarded
                    if not preserve_local_completion:
                        task.status = claimed_task.status
                        task.attempt_count = claimed_task.attempts
                    task.lease_owner = None
                    task.heartbeat_at = None
                    task.lease_expires_at = now_utc() if task.status == "running" else None
                for task_id, task in existing_tasks.items():
                    if task_id not in remote_ids:
                        task.status = "cancelled"
                        task.lease_owner = None
                        task.lease_expires_at = None
            elif is_new:
                for spec in expand_task_specs(snapshot):
                    session.add(
                        ProspectingTask(
                            run_id=run.id,
                            source=spec.source.value,
                            keyword=spec.keyword,
                            region_code=spec.region_code,
                            region_name=spec.region_name,
                            comuna_code=spec.comuna_code,
                            comuna_name=spec.comuna_name,
                            max_results=spec.max_results,
                            status="pending",
                            max_attempts=task_max_attempts,
                        )
                    )

            run.remote_candidates_baseline = max(
                run.remote_candidates_baseline or 0, claim.candidates_found
            )
            if worker_id is not None:
                run.crm_worker_id = worker_id
            run.crm_lease_token = claim.lease_token
            run.crm_lease_expires_at = claim.lease_expires_at
            run.status = "running"
            return str(run.id)

    async def claim_task(
        self, run_id: str, worker_id: str, lease_seconds: int
    ) -> WorkerTask | None:
        now = now_utc()
        async with self._sessions() as session, session.begin():
            exhausted = (
                await session.execute(
                    select(ProspectingTask)
                    .where(
                        ProspectingTask.run_id == uuid.UUID(run_id),
                        ProspectingTask.status == "running",
                        ProspectingTask.lease_expires_at < now,
                        ProspectingTask.attempt_count >= ProspectingTask.max_attempts,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
            for exhausted_task in exhausted:
                exhausted_task.status = "failed"
                exhausted_task.error_log = exhausted_task.error_log or "task lease expired"
                exhausted_task.lease_owner = None
                exhausted_task.lease_expires_at = None
            task = (
                await session.execute(
                    select(ProspectingTask)
                    .where(
                        ProspectingTask.run_id == uuid.UUID(run_id),
                        ProspectingTask.attempt_count < ProspectingTask.max_attempts,
                        ProspectingTask.available_at <= now,
                        or_(
                            ProspectingTask.status == "pending",
                            and_(
                                ProspectingTask.status == "running",
                                ProspectingTask.lease_expires_at < now,
                            ),
                        ),
                    )
                    .order_by(
                        case(
                            (ProspectingTask.source == SourceName.google_places.value, 0),
                            (ProspectingTask.source == SourceName.brave_search.value, 1),
                            else_=2,
                        ),
                        ProspectingTask.created_at,
                        ProspectingTask.id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if task is None:
                return None
            task.status = "running"
            task.attempt_count += 1
            task.lease_owner = worker_id
            task.heartbeat_at = now
            task.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return WorkerTask(
                id=str(task.id),
                run_id=run_id,
                source=SourceName(task.source),
                keyword=task.keyword,
                region_code=task.region_code,
                region_name=task.region_name,
                comuna_code=task.comuna_code,
                comuna_name=task.comuna_name,
                max_results=task.max_results,
                attempt_count=task.attempt_count,
                max_attempts=task.max_attempts,
            )

    async def partition_discoveries(
        self, run_id: str, candidates: list[ProspectCandidate]
    ) -> tuple[list[ProspectCandidate], list[ProspectCandidate]]:
        """Return novel hits and already-known hits merged with this run's evidence."""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(ProspectingCandidateRecord).where(
                        ProspectingCandidateRecord.run_id == uuid.UUID(run_id)
                    )
                )
            ).scalars().all()
            known = [ProspectCandidate.model_validate(row.payload) for row in rows]

        novel: list[ProspectCandidate] = []
        merged: list[ProspectCandidate] = []
        for incoming in candidates:
            match = match_candidate(incoming, known)
            existing = next(
                (item for item in known if item.candidate_id == match.matched_id), None
            )
            if match.disposition == DedupDisposition.exact_match and existing is not None:
                merged.append(merge_exact_candidate(existing, incoming))
            else:
                novel.append(incoming)
        return novel, merged

    async def heartbeat_task(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = now_utc()
        async with self._sessions() as session, session.begin():
            task = await session.get(ProspectingTask, uuid.UUID(task_id), with_for_update=True)
            if (
                task is None
                or task.status != "running"
                or task.lease_owner != worker_id
                or task.lease_expires_at is None
                or task.lease_expires_at <= now
            ):
                return False
            task.heartbeat_at = now
            task.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return True

    async def finish_task(
        self, task_id: str, worker_id: str, *, results: int, rejected: int
    ) -> None:
        async with self._sessions() as session, session.begin():
            task = await session.get(ProspectingTask, uuid.UUID(task_id), with_for_update=True)
            if task is None or task.lease_owner != worker_id or task.status != "running":
                raise RuntimeError("task lease lost")
            task.status = "completed"
            task.results_count = results
            task.rejected_count = rejected
            task.lease_owner = None
            task.lease_expires_at = None

    async def fail_task(self, task_id: str, worker_id: str, error: str) -> None:
        async with self._sessions() as session, session.begin():
            task = await session.get(ProspectingTask, uuid.UUID(task_id), with_for_update=True)
            if task is None or task.lease_owner != worker_id or task.status != "running":
                return
            task.error_log = error[:2000]
            task.status = "failed" if task.attempt_count >= task.max_attempts else "pending"
            task.available_at = now_utc()
            task.lease_owner = None
            task.lease_expires_at = None

    async def cancel_tasks(self, run_id: str) -> None:
        async with self._sessions() as session, session.begin():
            tasks = (
                await session.execute(
                    select(ProspectingTask)
                    .where(
                        ProspectingTask.run_id == uuid.UUID(run_id),
                        ProspectingTask.status.in_(("pending", "running")),
                    )
                    .with_for_update()
                )
            ).scalars()
            for task in tasks:
                task.status = "cancelled"
                task.lease_owner = None
                task.lease_expires_at = None

    async def save_candidates(
        self, run_id: str, task: WorkerTask, candidates: list[ProspectCandidate]
    ) -> list[ProspectCandidate]:
        accepted: list[ProspectCandidate] = []
        async with self._sessions() as session, session.begin():
            run_row = await session.get(ProspectingRun, uuid.UUID(run_id))
            if run_row is None:
                raise ValueError("prospecting run does not exist")
            snapshot = ProspectingRunSnapshot.model_validate(run_row.snapshot)
            rows = (await session.execute(select(ProspectingCandidateRecord))).scalars().all()
            known = [ProspectCandidate.model_validate(row.payload) for row in rows]
            rows_by_candidate_id: dict[str | None, list[ProspectingCandidateRecord]] = {}
            for candidate, row in zip(known, rows, strict=True):
                rows_by_candidate_id.setdefault(candidate.candidate_id, []).append(row)

            for raw_incoming in candidates:
                incoming = index_candidate_location_evidence(raw_incoming)
                match = match_candidate(incoming, known)
                if match.disposition == DedupDisposition.exact_match and match.matched_id:
                    matching_rows = rows_by_candidate_id[match.matched_id]
                    current_index = next(
                        (
                            index
                            for index, (value, existing_row) in enumerate(
                                zip(known, rows, strict=True)
                            )
                            if value.candidate_id == match.matched_id
                            and str(existing_row.run_id) == run_id
                        ),
                        None,
                    )
                    if current_index is not None:
                        prepared = merge_exact_candidate(known[current_index], incoming)
                        prepared = prepared.model_copy(update={"candidate_id": match.matched_id})
                        prepared = scope_candidate_locations(prepared, snapshot)
                        row = rows[current_index]
                        row.payload = prepared.model_dump(mode="json")
                        row.dedup_disposition = DedupDisposition.exact_match.value
                        known[current_index] = prepared
                    else:
                        # Global identity links the entity, but fields and
                        # evidence from another run never enter this payload.
                        prepared = incoming.model_copy(
                            update={
                                "candidate_id": match.matched_id,
                                "dedup_disposition": DedupDisposition.exact_match,
                            }
                        )
                        prepared = scope_candidate_locations(prepared, snapshot)
                        row = ProspectingCandidateRecord(
                            run_id=uuid.UUID(run_id),
                            candidate_key=candidate_fingerprint(incoming),
                            payload=prepared.model_dump(mode="json"),
                            dedup_disposition=DedupDisposition.exact_match.value,
                        )
                        session.add(row)
                        await session.flush()
                        matching_rows.append(row)
                        rows.append(row)
                        known.append(prepared)
                    accepted.append(prepared)
                else:
                    candidate_id = candidate_id_for_match(incoming, match)
                    current_index = next(
                        (
                            index
                            for index, (value, existing_row) in enumerate(
                                zip(known, rows, strict=True)
                            )
                            if value.candidate_id == candidate_id
                            and str(existing_row.run_id) == run_id
                        ),
                        None,
                    )
                    if current_index is not None:
                        prepared = merge_exact_candidate(known[current_index], incoming)
                        row = rows[current_index]
                    else:
                        prepared = incoming
                        row = None
                    prepared = prepared.model_copy(
                        update={
                            "candidate_id": candidate_id,
                            "dedup_disposition": match.disposition,
                            "possible_duplicate_of": match.matched_id,
                        }
                    )
                    prepared = scope_candidate_locations(prepared, snapshot)
                    if row is None:
                        row = ProspectingCandidateRecord(
                            run_id=uuid.UUID(run_id),
                            candidate_key=(
                                review_candidate_fingerprint(prepared)
                                if match.disposition == DedupDisposition.possible_duplicate
                                else candidate_fingerprint(prepared)
                            ),
                            payload=prepared.model_dump(mode="json"),
                            dedup_disposition=match.disposition.value,
                            possible_duplicate_of=(
                                rows_by_candidate_id[match.matched_id][0].id
                                if match.matched_id in rows_by_candidate_id
                                else None
                            ),
                        )
                        session.add(row)
                        await session.flush()
                        known.append(prepared)
                        rows.append(row)
                        rows_by_candidate_id.setdefault(candidate_id, []).append(row)
                    else:
                        row.payload = prepared.model_dump(mode="json")
                        row.dedup_disposition = match.disposition.value
                        known[current_index] = prepared
                    accepted.append(prepared)

                for evidence in accepted[-1].evidence:
                    session.add(
                        ProspectEvidenceRecord(
                            candidate_id=row.id,
                            provider=evidence.provider.value,
                            provider_record_id=evidence.provider_record_id,
                            source_url=evidence.source_url,
                            field=evidence.field,
                            value=evidence.value,
                            confidence=evidence.confidence,
                            observed_at=evidence.observed_at,
                            retention_until=evidence.retention_until,
                        )
                    )

            if accepted:
                await self._queue_outbox(
                    session,
                    run_id,
                    "candidates",
                    f"{run_id}:{task.id}:{task.attempt_count}:candidates",
                    [candidate.model_dump(mode="json") for candidate in accepted],
                )
        return accepted

    async def save_event(self, run_id: str, task: WorkerTask | None, event: RunEvent) -> None:
        async with self._sessions() as session, session.begin():
            existing = await session.scalar(
                select(ProspectingEventRecord.id).where(
                    ProspectingEventRecord.event_id == event.event_id
                )
            )
            if existing is None:
                session.add(
                    ProspectingEventRecord(
                        run_id=uuid.UUID(run_id),
                        event_id=event.event_id,
                        task_id=uuid.UUID(task.id) if task else None,
                        level=event.level.value,
                        stage=event.stage,
                        message=event.message,
                        metrics=event.metrics,
                        occurred_at=event.occurred_at,
                    )
                )
            await self._queue_outbox(
                session,
                run_id,
                "events",
                f"{run_id}:{event.event_id}:event",
                [event.model_dump(mode="json")],
            )

    @staticmethod
    async def _queue_outbox(
        session: AsyncSession, run_id: str, kind: str, key: str, payload: dict | list
    ) -> None:
        exists = await session.scalar(
            select(CRMOutboxMessage.id).where(CRMOutboxMessage.idempotency_key == key)
        )
        if exists is None:
            session.add(
                CRMOutboxMessage(
                    run_id=uuid.UUID(run_id), kind=kind, idempotency_key=key, payload=payload
                )
            )

    async def get_outbox(self, run_id: str, limit: int = 50) -> list[OutboxEnvelope]:
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(CRMOutboxMessage)
                        .where(
                            CRMOutboxMessage.run_id == uuid.UUID(run_id),
                            CRMOutboxMessage.status == "queued",
                            CRMOutboxMessage.available_at <= now_utc(),
                        )
                        .order_by(CRMOutboxMessage.created_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [
                OutboxEnvelope(
                    id=str(row.id),
                    run_id=run_id,
                    kind=row.kind,
                    idempotency_key=row.idempotency_key,
                    payload=row.payload,
                )
                for row in rows
            ]

    async def has_pending_outbox(self, run_id: str) -> bool:
        async with self._sessions() as session:
            return bool(
                await session.scalar(
                    select(CRMOutboxMessage.id).where(
                        CRMOutboxMessage.run_id == uuid.UUID(run_id),
                        CRMOutboxMessage.status == "queued",
                    )
                )
            )

    async def pending_terminal_replays(self, limit: int = 50) -> list[TerminalOutboxReplay]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(CRMOutboxMessage, ProspectingRun)
                    .join(ProspectingRun, CRMOutboxMessage.run_id == ProspectingRun.id)
                    .where(
                        CRMOutboxMessage.status == "queued",
                        CRMOutboxMessage.kind.in_(("complete", "fail")),
                        ProspectingRun.crm_worker_id.is_not(None),
                        ProspectingRun.crm_lease_token.is_not(None),
                    )
                    .order_by(CRMOutboxMessage.created_at)
                    .limit(limit)
                )
            ).all()
            return [
                TerminalOutboxReplay(
                    local_run_id=str(run.id),
                    crm_run_id=run.crm_run_id,
                    worker_id=str(run.crm_worker_id),
                    lease_token=str(run.crm_lease_token),
                    message=OutboxEnvelope(
                        id=str(message.id),
                        run_id=str(run.id),
                        kind=message.kind,
                        idempotency_key=message.idempotency_key,
                        payload=message.payload,
                    ),
                )
                for message, run in rows
            ]

    async def mark_outbox_delivered(self, message_id: str) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(CRMOutboxMessage, uuid.UUID(message_id), with_for_update=True)
            if row:
                row.status = "delivered"
                row.delivered_at = now_utc()
                row.last_error = None

    async def finalize_terminal_outbox(
        self,
        run_id: str,
        message_id: str,
        status: str,
        *,
        stats: dict | None = None,
        error: str | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(ProspectingRun, uuid.UUID(run_id), with_for_update=True)
            message = await session.get(
                CRMOutboxMessage, uuid.UUID(message_id), with_for_update=True
            )
            if run is None or message is None:
                return
            message.status = "delivered"
            message.delivered_at = now_utc()
            message.last_error = None
            run.status = status
            run.stats = stats
            run.error_log = error
            run.finished_at = now_utc()

    async def mark_outbox_failed(self, message_id: str, error: str, *, retryable: bool) -> bool:
        async with self._sessions() as session, session.begin():
            row = await session.get(CRMOutboxMessage, uuid.UUID(message_id), with_for_update=True)
            if row is None:
                return False
            row.attempt_count += 1
            row.last_error = error[:2000]
            # Network/425/429/5xx failures remain recoverable indefinitely;
            # max_attempts controls backoff saturation, not data loss.
            dead = not retryable
            row.status = "dead" if dead else "queued"
            retry_delay = min(300, 2 ** min(row.attempt_count, 8)) if not dead else 0
            row.available_at = now_utc() + timedelta(seconds=retry_delay)
            return dead

    async def split_candidate_outbox(self, message_id: str, error: str) -> bool:
        """Atomically replace a rejected candidate batch with two smaller batches."""

        async with self._sessions() as session, session.begin():
            row = await session.get(CRMOutboxMessage, uuid.UUID(message_id), with_for_update=True)
            if (
                row is None
                or row.kind != "candidates"
                or row.status not in {"queued", "dead"}
                or not isinstance(row.payload, list)
                or len(row.payload) < 2
            ):
                return False

            midpoint = len(row.payload) // 2
            parts = (row.payload[:midpoint], row.payload[midpoint:])
            for index, payload in enumerate(parts):
                key = f"{row.id}:split:{index}"
                exists = await session.scalar(
                    select(CRMOutboxMessage.id).where(CRMOutboxMessage.idempotency_key == key)
                )
                if exists is None:
                    session.add(
                        CRMOutboxMessage(
                            run_id=row.run_id,
                            kind="candidates",
                            idempotency_key=key,
                            payload=payload,
                        )
                    )

            row.status = "split"
            row.delivered_at = now_utc()
            row.last_error = error[:2000]
            return True

    async def discard_candidate_outbox(self, message_id: str, error: str) -> bool:
        """Audit and skip one CRM-rejected candidate without stopping the run."""

        async with self._sessions() as session, session.begin():
            row = await session.get(CRMOutboxMessage, uuid.UUID(message_id), with_for_update=True)
            if (
                row is None
                or row.kind != "candidates"
                or row.status not in {"queued", "dead"}
                or not isinstance(row.payload, list)
                or len(row.payload) != 1
            ):
                return False
            run = await session.get(ProspectingRun, row.run_id, with_for_update=True)
            row.status = "discarded"
            row.delivered_at = now_utc()
            row.last_error = error[:2000]
            if run is not None:
                stats = dict(run.stats or {})
                stats["rejected_invalid"] = int(stats.get("rejected_invalid", 0)) + 1
                run.stats = stats
            return True

    async def record_candidate_ack(
        self, run_id: str, message_id: str, ack: CandidateBatchAck
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(ProspectingRun, uuid.UUID(run_id), with_for_update=True)
            message = await session.get(
                CRMOutboxMessage, uuid.UUID(message_id), with_for_update=True
            )
            if run is None or message is None or message.status == "delivered":
                return
            acknowledged_total = (
                ack.candidates_found
                if ack.candidates_found is not None
                else (run.remote_candidates_baseline or 0) + ack.accepted
            )
            run.remote_candidates_baseline = max(
                run.remote_candidates_baseline or 0, acknowledged_total
            )
            stats = dict(run.stats or {})
            stats["crm_accepted"] = int(stats.get("crm_accepted", 0)) + ack.accepted
            stats["rejected_limit"] = int(stats.get("rejected_limit", 0)) + ack.rejected_limit
            run.stats = stats
            message.status = "delivered"
            message.delivered_at = now_utc()
            message.last_error = None

    async def queue_terminal(
        self, run_id: str, kind: str, payload: dict, idempotency_key: str
    ) -> None:
        async with self._sessions() as session, session.begin():
            await self._queue_outbox(session, run_id, kind, idempotency_key, payload)

    async def summary(self, run_id: str) -> RunSummary:
        async with self._sessions() as session:
            status_rows = (
                await session.execute(
                    select(ProspectingTask.status, func.count(ProspectingTask.id))
                    .where(ProspectingTask.run_id == uuid.UUID(run_id))
                    .group_by(ProspectingTask.status)
                )
            ).all()
            counts = dict(status_rows)
            run = await session.get(ProspectingRun, uuid.UUID(run_id))
            dead_letters = await session.scalar(
                select(func.count(CRMOutboxMessage.id)).where(
                    CRMOutboxMessage.run_id == uuid.UUID(run_id),
                    CRMOutboxMessage.status == "dead",
                )
            )
            budget_limited = await session.scalar(
                select(func.count(ProspectingEventRecord.id)).where(
                    ProspectingEventRecord.run_id == uuid.UUID(run_id),
                    ProspectingEventRecord.metrics["budget_limited"].as_boolean().is_(True),
                )
            )
            return RunSummary(
                pending=counts.get("pending", 0),
                running=counts.get("running", 0),
                completed=counts.get("completed", 0),
                failed=counts.get("failed", 0),
                cancelled=counts.get("cancelled", 0),
                candidates=run.remote_candidates_baseline if run else 0,
                dead_letters=dead_letters or 0,
                crm_accepted=int((run.stats or {}).get("crm_accepted", 0)) if run else 0,
                rejected_limit=int((run.stats or {}).get("rejected_limit", 0)) if run else 0,
                rejected_invalid=int((run.stats or {}).get("rejected_invalid", 0)) if run else 0,
                budget_limited=budget_limited or 0,
            )

    async def set_run_status(
        self, run_id: str, status: str, *, stats: dict | None = None, error: str | None = None
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(ProspectingRun, uuid.UUID(run_id), with_for_update=True)
            if run:
                run.status = status
                run.stats = stats
                run.error_log = error
                if status in {"completed", "partial", "failed", "cancelled"}:
                    run.finished_at = now_utc()


@dataclass
class _MemoryTask:
    task: WorkerTask
    status: str = "pending"
    max_attempts: int = 3
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    results: int = 0
    rejected: int = 0
    error: str | None = None


@dataclass
class _MemoryRun:
    snapshot: ProspectingRunSnapshot
    tasks: dict[str, _MemoryTask] = field(default_factory=dict)
    candidates: dict[str, ProspectCandidate] = field(default_factory=dict)
    outbox: dict[str, OutboxEnvelope] = field(default_factory=dict)
    outbox_delivered: set[str] = field(default_factory=set)
    outbox_dead: set[str] = field(default_factory=set)
    outbox_attempts: dict[str, int] = field(default_factory=dict)
    outbox_available_at: dict[str, datetime] = field(default_factory=dict)
    remote_candidates_baseline: int = 0
    crm_accepted: int = 0
    rejected_limit: int = 0
    rejected_invalid: int = 0
    status: str = "running"
    crm_worker_id: str | None = None
    crm_lease_token: str | None = None


class MemoryWorkerStore:
    """Deterministic store for development/tests; mirrors the SQL semantics."""

    def __init__(self) -> None:
        self.runs: dict[str, _MemoryRun] = {}
        self._lock = __import__("asyncio").Lock()

    async def ensure_run(
        self,
        claim: ClaimedRun,
        task_max_attempts: int,
        worker_id: str | None = None,
    ) -> str:
        async with self._lock:
            run_id = claim.snapshot.crm_run_id
            is_new = run_id not in self.runs
            if is_new:
                run = _MemoryRun(snapshot=claim.snapshot)
                self.runs[run_id] = run
            else:
                run = self.runs[run_id]

            queued_completion_task_ids = _completed_task_ids_from_event_payloads(
                [
                    message.payload
                    for key, message in run.outbox.items()
                    if key not in run.outbox_delivered
                    and key not in run.outbox_dead
                    and message.kind == "events"
                ]
            )

            if claim.tasks is not None:
                remote_ids: set[str] = set()
                for spec in claim.tasks:
                    task_id = spec.task_id
                    remote_ids.add(task_id)
                    max_attempts = spec.max_attempts
                    territory = validate_claimed_task(claim.snapshot, spec)
                    existing = run.tasks.get(task_id)
                    preserve_local_completion = bool(
                        existing
                        and existing.status == "completed"
                        and spec.status in {"pending", "running"}
                        and task_id in queued_completion_task_ids
                    )
                    status = existing.status if preserve_local_completion else spec.status
                    attempts = (
                        existing.task.attempt_count if preserve_local_completion else spec.attempts
                    )
                    run.tasks[task_id] = _MemoryTask(
                        task=WorkerTask(
                            id=task_id,
                            run_id=run_id,
                            source=spec.source,
                            keyword=spec.keyword,
                            region_code=spec.region_code,
                            region_name=spec.region_name or territory.region_name,
                            comuna_code=spec.comuna_code,
                            comuna_name=spec.comuna_name or territory.comuna_name,
                            max_results=spec.max_results,
                            attempt_count=attempts,
                            max_attempts=max_attempts,
                        ),
                        status=status,
                        max_attempts=max_attempts,
                        lease_expires_at=now_utc() if status == "running" else None,
                        results=spec.candidates_found,
                        rejected=spec.results_discarded,
                    )
                for task_id, record in run.tasks.items():
                    if task_id not in remote_ids:
                        record.status = "cancelled"
                        record.lease_owner = None
                        record.lease_expires_at = None
            elif is_new:
                specs = tuple(expand_task_specs(claim.snapshot))
                for index, spec in enumerate(specs, start=1):
                    task_id = getattr(spec, "task_id", f"{run_id}-task-{index}")
                    max_attempts = getattr(spec, "max_attempts", task_max_attempts)
                    territory = next(
                        (
                            item
                            for item in claim.snapshot.campaign.territories
                            if item.region_code == spec.region_code
                            and item.comuna_code == spec.comuna_code
                        ),
                        None,
                    )
                    if territory is None:
                        raise ValueError("claimed task is outside the run snapshot")
                    run.tasks[task_id] = _MemoryTask(
                        task=WorkerTask(
                            id=task_id,
                            run_id=run_id,
                            source=spec.source,
                            keyword=spec.keyword,
                            region_code=spec.region_code,
                            region_name=spec.region_name or territory.region_name,
                            comuna_code=spec.comuna_code,
                            comuna_name=spec.comuna_name or territory.comuna_name,
                            max_results=spec.max_results,
                            attempt_count=getattr(spec, "attempts", 0),
                            max_attempts=max_attempts,
                        ),
                        max_attempts=max_attempts,
                    )
            run.remote_candidates_baseline = max(
                run.remote_candidates_baseline, claim.candidates_found
            )
            if worker_id is not None:
                run.crm_worker_id = worker_id
            run.crm_lease_token = claim.lease_token
            return run_id

    async def claim_task(
        self, run_id: str, worker_id: str, lease_seconds: int
    ) -> WorkerTask | None:
        now = now_utc()
        async with self._lock:
            source_priority = {
                SourceName.google_places: 0,
                SourceName.brave_search: 1,
            }
            records = sorted(
                self.runs[run_id].tasks.values(),
                key=lambda item: source_priority.get(item.task.source, 2),
            )
            for record in records:
                if (
                    record.status == "running"
                    and record.lease_expires_at is not None
                    and record.lease_expires_at <= now
                    and record.task.attempt_count >= record.max_attempts
                ):
                    record.status = "failed"
                    record.error = record.error or "task lease expired"
                    record.lease_owner = None
                    continue
                claimable = record.status == "pending" or (
                    record.status == "running"
                    and record.lease_expires_at is not None
                    and record.lease_expires_at <= now
                )
                if not claimable or record.task.attempt_count >= record.max_attempts:
                    continue
                task = WorkerTask(
                    **{
                        **record.task.__dict__,
                        "attempt_count": record.task.attempt_count + 1,
                    }
                )
                record.task = task
                record.status = "running"
                record.lease_owner = worker_id
                record.lease_expires_at = now + timedelta(seconds=lease_seconds)
                return task
        return None

    async def heartbeat_task(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        async with self._lock:
            record = next(
                (run.tasks[task_id] for run in self.runs.values() if task_id in run.tasks), None
            )
            if not record or record.status != "running" or record.lease_owner != worker_id:
                return False
            record.lease_expires_at = now_utc() + timedelta(seconds=lease_seconds)
            return True

    def _task_record(self, task_id: str) -> _MemoryTask:
        return next(run.tasks[task_id] for run in self.runs.values() if task_id in run.tasks)

    async def finish_task(
        self, task_id: str, worker_id: str, *, results: int, rejected: int
    ) -> None:
        async with self._lock:
            record = self._task_record(task_id)
            if record.lease_owner != worker_id:
                raise RuntimeError("task lease lost")
            record.status = "completed"
            record.results = results
            record.rejected = rejected
            record.lease_owner = None

    async def fail_task(self, task_id: str, worker_id: str, error: str) -> None:
        async with self._lock:
            record = self._task_record(task_id)
            if record.lease_owner != worker_id:
                return
            record.error = error
            record.status = (
                "failed" if record.task.attempt_count >= record.max_attempts else "pending"
            )
            record.lease_owner = None

    async def cancel_tasks(self, run_id: str) -> None:
        async with self._lock:
            for record in self.runs[run_id].tasks.values():
                if record.status in {"pending", "running"}:
                    record.status = "cancelled"

    async def save_candidates(
        self, run_id: str, task: WorkerTask, candidates: list[ProspectCandidate]
    ) -> list[ProspectCandidate]:
        async with self._lock:
            all_candidates = [c for run in self.runs.values() for c in run.candidates.values()]
            snapshot = self.runs[run_id].snapshot
            accepted: list[ProspectCandidate] = []
            for raw_incoming in candidates:
                incoming = index_candidate_location_evidence(raw_incoming)
                match = match_candidate(incoming, all_candidates)
                if match.disposition == DedupDisposition.exact_match and match.matched_id:
                    current = self.runs[run_id].candidates.get(match.matched_id)
                    if current is not None:
                        prepared = merge_exact_candidate(current, incoming).model_copy(
                            update={"candidate_id": match.matched_id}
                        )
                    else:
                        prepared = incoming.model_copy(
                            update={
                                "candidate_id": match.matched_id,
                                "dedup_disposition": DedupDisposition.exact_match,
                            }
                        )
                else:
                    candidate_id = candidate_id_for_match(incoming, match)
                    current_review = self.runs[run_id].candidates.get(candidate_id)
                    prepared = (
                        merge_exact_candidate(current_review, incoming)
                        if current_review is not None
                        else incoming
                    )
                    prepared = prepared.model_copy(
                        update={
                            "candidate_id": candidate_id,
                            "dedup_disposition": match.disposition,
                            "possible_duplicate_of": match.matched_id,
                        }
                    )
                prepared = scope_candidate_locations(prepared, snapshot)
                self.runs[run_id].candidates[prepared.candidate_id or ""] = prepared
                accepted.append(prepared)
                all_candidates.append(prepared)
            if accepted:
                key = f"{run_id}:{task.id}:{task.attempt_count}:candidates"
                self.runs[run_id].outbox[key] = OutboxEnvelope(
                    id=key,
                    run_id=run_id,
                    kind="candidates",
                    idempotency_key=key,
                    payload=[c.model_dump(mode="json") for c in accepted],
                )
            return accepted

    async def partition_discoveries(
        self, run_id: str, candidates: list[ProspectCandidate]
    ) -> tuple[list[ProspectCandidate], list[ProspectCandidate]]:
        async with self._lock:
            known = list(self.runs[run_id].candidates.values())
            novel: list[ProspectCandidate] = []
            merged: list[ProspectCandidate] = []
            for incoming in candidates:
                match = match_candidate(incoming, known)
                existing = next(
                    (item for item in known if item.candidate_id == match.matched_id), None
                )
                if match.disposition == DedupDisposition.exact_match and existing is not None:
                    merged.append(merge_exact_candidate(existing, incoming))
                else:
                    novel.append(incoming)
            return novel, merged

    async def save_event(self, run_id: str, task: WorkerTask | None, event: RunEvent) -> None:
        del task
        async with self._lock:
            key = f"{run_id}:{event.event_id}:event"
            self.runs[run_id].outbox[key] = OutboxEnvelope(
                id=key,
                run_id=run_id,
                kind="events",
                idempotency_key=key,
                payload=[event.model_dump(mode="json")],
            )

    async def get_outbox(self, run_id: str, limit: int = 50) -> list[OutboxEnvelope]:
        async with self._lock:
            run = self.runs[run_id]
            return [
                message
                for key, message in run.outbox.items()
                if key not in run.outbox_delivered
                and key not in run.outbox_dead
                and run.outbox_available_at.get(key, datetime.min.replace(tzinfo=timezone.utc))
                <= now_utc()
            ][:limit]

    async def has_pending_outbox(self, run_id: str) -> bool:
        async with self._lock:
            run = self.runs[run_id]
            return any(
                key not in run.outbox_delivered and key not in run.outbox_dead for key in run.outbox
            )

    async def pending_terminal_replays(self, limit: int = 50) -> list[TerminalOutboxReplay]:
        async with self._lock:
            replays: list[TerminalOutboxReplay] = []
            for run_id, run in self.runs.items():
                if not run.crm_worker_id or not run.crm_lease_token:
                    continue
                for key, message in run.outbox.items():
                    if (
                        key in run.outbox_delivered
                        or key in run.outbox_dead
                        or message.kind not in {"complete", "fail"}
                    ):
                        continue
                    replays.append(
                        TerminalOutboxReplay(
                            local_run_id=run_id,
                            crm_run_id=run.snapshot.crm_run_id,
                            worker_id=run.crm_worker_id,
                            lease_token=run.crm_lease_token,
                            message=message,
                        )
                    )
                    if len(replays) >= limit:
                        return replays
            return replays

    async def mark_outbox_delivered(self, message_id: str) -> None:
        async with self._lock:
            for run in self.runs.values():
                if message_id in run.outbox:
                    run.outbox_delivered.add(message_id)

    async def finalize_terminal_outbox(
        self,
        run_id: str,
        message_id: str,
        status: str,
        *,
        stats: dict | None = None,
        error: str | None = None,
    ) -> None:
        del stats, error
        async with self._lock:
            run = self.runs[run_id]
            run.outbox_delivered.add(message_id)
            run.status = status

    async def mark_outbox_failed(self, message_id: str, error: str, *, retryable: bool) -> bool:
        del error
        async with self._lock:
            for run in self.runs.values():
                if message_id not in run.outbox:
                    continue
                attempts = run.outbox_attempts.get(message_id, 0) + 1
                run.outbox_attempts[message_id] = attempts
                dead = not retryable
                if dead:
                    run.outbox_dead.add(message_id)
                else:
                    run.outbox_available_at[message_id] = now_utc() + timedelta(
                        seconds=min(300, 2 ** min(attempts, 8))
                    )
                return dead
        return False

    async def split_candidate_outbox(self, message_id: str, error: str) -> bool:
        del error
        async with self._lock:
            for run in self.runs.values():
                message = run.outbox.get(message_id)
                if (
                    message is None
                    or message.kind != "candidates"
                    or message_id in run.outbox_delivered
                    or not isinstance(message.payload, list)
                    or len(message.payload) < 2
                ):
                    continue
                midpoint = len(message.payload) // 2
                for index, payload in enumerate(
                    (message.payload[:midpoint], message.payload[midpoint:])
                ):
                    key = f"{message.id}:split:{index}"
                    run.outbox.setdefault(
                        key,
                        OutboxEnvelope(
                            id=key,
                            run_id=message.run_id,
                            kind="candidates",
                            idempotency_key=key,
                            payload=payload,
                        ),
                    )
                run.outbox_delivered.add(message_id)
                run.outbox_dead.discard(message_id)
                return True
        return False

    async def discard_candidate_outbox(self, message_id: str, error: str) -> bool:
        del error
        async with self._lock:
            for run in self.runs.values():
                message = run.outbox.get(message_id)
                if (
                    message is None
                    or message.kind != "candidates"
                    or not isinstance(message.payload, list)
                    or len(message.payload) != 1
                ):
                    continue
                run.outbox_delivered.add(message_id)
                run.outbox_dead.discard(message_id)
                run.rejected_invalid += 1
                return True
        return False

    async def record_candidate_ack(
        self, run_id: str, message_id: str, ack: CandidateBatchAck
    ) -> None:
        async with self._lock:
            run = self.runs[run_id]
            if message_id in run.outbox_delivered:
                return
            acknowledged_total = (
                ack.candidates_found
                if ack.candidates_found is not None
                else run.remote_candidates_baseline + ack.accepted
            )
            run.remote_candidates_baseline = max(run.remote_candidates_baseline, acknowledged_total)
            run.crm_accepted += ack.accepted
            run.rejected_limit += ack.rejected_limit
            run.outbox_delivered.add(message_id)

    async def queue_terminal(
        self, run_id: str, kind: str, payload: dict, idempotency_key: str
    ) -> None:
        async with self._lock:
            self.runs[run_id].outbox[idempotency_key] = OutboxEnvelope(
                id=idempotency_key,
                run_id=run_id,
                kind=kind,
                idempotency_key=idempotency_key,
                payload=payload,
            )

    async def summary(self, run_id: str) -> RunSummary:
        async with self._lock:
            run = self.runs[run_id]
            counts = {
                status: 0 for status in ("pending", "running", "completed", "failed", "cancelled")
            }
            for record in run.tasks.values():
                counts[record.status] += 1
            return RunSummary(
                **counts,
                candidates=run.remote_candidates_baseline,
                dead_letters=len(run.outbox_dead),
                crm_accepted=run.crm_accepted,
                rejected_limit=run.rejected_limit,
                rejected_invalid=run.rejected_invalid,
                budget_limited=sum(
                    1
                    for message in run.outbox.values()
                    if message.kind == "events"
                    for event in (message.payload if isinstance(message.payload, list) else [])
                    if isinstance(event, dict)
                    and bool((event.get("metrics") or {}).get("budget_limited"))
                ),
            )

    async def set_run_status(
        self, run_id: str, status: str, *, stats: dict | None = None, error: str | None = None
    ) -> None:
        del stats, error
        async with self._lock:
            self.runs[run_id].status = status
