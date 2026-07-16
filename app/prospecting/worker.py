from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass

from pydantic import TypeAdapter

from app.config import get_settings
from app.crm.port import (
    CRMIdempotencyConflict,
    CRMLeaseLostError,
    CRMPermanentError,
    CRMPort,
    CRMRetryableError,
    HeartbeatResult,
)
from app.prospecting.contracts import (
    ClaimedRun,
    CandidateBatchAck,
    CompletionReport,
    EventLevel,
    ProspectCandidate,
    RunEvent,
    RunStatus,
    SourceName,
)
from app.prospecting.budget import estimate_google_run
from app.prospecting.sources import SourceExecutor, SourceSearchResult
from app.prospecting.scoring import classify_and_score
from app.prospecting.store import (
    OutboxEnvelope,
    TerminalOutboxReplay,
    WorkerStore,
    WorkerTask,
)
from app.prospecting.validation import (
    sanitize_unsubstantiated_external_fields,
    validate_candidate,
)

logger = logging.getLogger("clima_activa.prospecting_worker")


@dataclass(frozen=True)
class WorkerConfig:
    poll_seconds: float = 15
    lease_seconds: int = 120
    heartbeat_seconds: float = 30
    task_max_attempts: int = 3


class ProspectingWorker:
    def __init__(
        self,
        crm: CRMPort,
        store: WorkerStore,
        sources: SourceExecutor,
        *,
        worker_id: str | None = None,
        config: WorkerConfig | None = None,
    ) -> None:
        self.crm = crm
        self.store = store
        self.sources = sources
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self.config = config or WorkerConfig()

    async def poll_once(self) -> bool:
        reconciled_terminal = await self._reconcile_terminal_outbox()
        claim = await self.crm.claim_run(self.worker_id, self.config.lease_seconds)
        if claim is None:
            claim_enrichment = getattr(self.crm, "claim_enrichment", None)
            if claim_enrichment is None:
                return reconciled_terminal
            enrichment = await claim_enrichment(self.worker_id, 300)
            if enrichment is None:
                return reconciled_terminal
            await self._process_enrichment(enrichment)
            return True
        await self._process_claim(claim)
        return True

    async def _process_enrichment(self, claim) -> None:
        try:
            enrich = getattr(self.sources, "enrich_existing", None)
            if enrich is None:
                raise RuntimeError("The configured source executor cannot enrich existing candidates")
            candidate, summary = await enrich(claim.candidate, claim.run_id)
            await self.crm.complete_enrichment(
                claim,
                candidate,
                summary,
                f"enrichment:{claim.job_id}:attempt:{claim.lease_token}:complete",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - CRM owns bounded retries
            logger.warning("candidate enrichment failed job=%s error=%s", claim.job_id, type(exc).__name__)
            await self.crm.fail_enrichment(
                claim,
                f"{type(exc).__name__}: {str(exc)[:1000]}",
                f"enrichment:{claim.job_id}:attempt:{claim.lease_token}:fail",
            )

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - daemon keeps polling after isolated failures
                logger.exception("prospecting poll failed")
            await asyncio.sleep(self.config.poll_seconds)

    async def _process_claim(self, claim: ClaimedRun) -> None:
        snapshot = claim.snapshot
        local_run_id = await self.store.ensure_run(
            claim,
            self.config.task_max_attempts,
            worker_id=self.worker_id,
        )
        lease_lost = asyncio.Event()
        cancel_requested = asyncio.Event()
        run_heartbeat = asyncio.create_task(
            self._run_heartbeat(claim, lease_lost, cancel_requested)
        )
        try:
            if not await self._deliver_outbox(local_run_id, claim):
                return
            while not lease_lost.is_set():
                if cancel_requested.is_set() or await self.crm.is_cancel_requested(
                    snapshot.crm_run_id, claim.lease_token
                ):
                    await self._cancel(local_run_id, claim)
                    return

                summary = await self.store.summary(local_run_id)
                if summary.dead_letters:
                    await self.store.cancel_tasks(local_run_id)
                    await self._complete(
                        local_run_id,
                        claim,
                        RunStatus.partial,
                        extra_stats={"delivery_failed": True},
                    )
                    return
                if summary.candidates >= snapshot.campaign.max_candidates:
                    await self.store.cancel_tasks(local_run_id)
                    terminal = (
                        RunStatus.partial
                        if summary.failed or summary.dead_letters or summary.rejected_invalid
                        else RunStatus.completed
                    )
                    await self._complete(
                        local_run_id,
                        claim,
                        terminal,
                        extra_stats={"limit_reached": True},
                    )
                    return

                task = await self.store.claim_task(
                    local_run_id, self.worker_id, self.config.lease_seconds
                )
                if task is None:
                    summary = await self.store.summary(local_run_id)
                    if summary.work_remaining:
                        await asyncio.sleep(min(1.0, self.config.poll_seconds))
                        continue
                    status = (
                        RunStatus.partial
                        if (
                            summary.failed
                            or summary.dead_letters
                            or summary.rejected_invalid
                            or summary.budget_limited
                        )
                        else RunStatus.completed
                    )
                    await self._complete(local_run_id, claim, status)
                    return

                heartbeat = await self._renew_run_lease(claim)
                if not heartbeat:
                    lease_lost.set()
                    return
                if heartbeat.cancel_requested or await self.crm.is_cancel_requested(
                    snapshot.crm_run_id, claim.lease_token
                ):
                    cancel_requested.set()
                    await self._cancel(local_run_id, claim)
                    return
                await self._execute_task(
                    local_run_id,
                    claim,
                    task,
                    lease_lost,
                    cancel_requested,
                )
                if cancel_requested.is_set():
                    await self._cancel(local_run_id, claim)
                    return
                if lease_lost.is_set() or not await self._deliver_outbox(local_run_id, claim):
                    return
        finally:
            run_heartbeat.cancel()
            await asyncio.gather(run_heartbeat, return_exceptions=True)

    async def _run_heartbeat(
        self,
        claim: ClaimedRun,
        lease_lost: asyncio.Event,
        cancel_requested: asyncio.Event | None = None,
    ) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_seconds)
            heartbeat = await self._renew_run_lease(claim)
            if not heartbeat:
                lease_lost.set()
                return
            if heartbeat.cancel_requested:
                if cancel_requested is not None:
                    cancel_requested.set()
                return

    async def _renew_run_lease(self, claim: ClaimedRun) -> HeartbeatResult:
        try:
            result = await self.crm.heartbeat(
                claim.snapshot.crm_run_id,
                claim.lease_token,
                self.config.lease_seconds,
            )
            if isinstance(result, HeartbeatResult):
                return result
            # Backward-compatible normalization for local test/development
            # ports written before heartbeat cancellation was added.
            return HeartbeatResult(lease_valid=bool(result))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - connectivity loss means lease is unsafe
            logger.warning("CRM heartbeat failed safely: %s", type(exc).__name__)
            return HeartbeatResult(lease_valid=False)

    async def _task_heartbeat(self, task: WorkerTask, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_seconds)
            ok = await self.store.heartbeat_task(task.id, self.worker_id, self.config.lease_seconds)
            if not ok:
                lost.set()
                return

    async def _execute_task(
        self,
        local_run_id: str,
        claim: ClaimedRun,
        task: WorkerTask,
        lease_lost: asyncio.Event,
        cancel_requested: asyncio.Event,
    ) -> None:
        task_lease_lost = asyncio.Event()
        task_heartbeat = asyncio.create_task(self._task_heartbeat(task, task_lease_lost))
        try:
            cost_estimate = (
                estimate_google_run(claim.snapshot, get_settings())
                if task.source.value == "google_places"
                else {}
            )
            await self.store.save_event(
                local_run_id,
                task,
                RunEvent(
                    event_id=str(uuid.uuid4()),
                    run_id=claim.snapshot.crm_run_id,
                    task_id=task.id,
                    source=task.source,
                    keyword=task.keyword,
                    comuna_code=task.comuna_code,
                    comuna_name=task.comuna_name,
                    task_status="running",
                    stage="task_started",
                    message=f"Consultando {task.source.value} en {task.comuna_name}",
                    metrics={
                        "attempt": task.attempt_count,
                        **cost_estimate,
                        "run_budget_usd": get_settings().google_places_run_budget_usd,
                        "daily_budget_usd": get_settings().google_places_daily_budget_usd,
                        "budget_alert_percent": int(
                            get_settings().google_places_budget_alert_ratio * 100
                        ),
                    },
                ),
            )
            if not await self._deliver_outbox(local_run_id, claim):
                lease_lost.set()
                return
            if cancel_requested.is_set():
                return
            search_result = await self._search_until_stopped(
                task,
                claim,
                task_lease_lost,
                lease_lost,
                cancel_requested,
            )
            if search_result is None:
                return
            if isinstance(search_result, SourceSearchResult):
                candidates = list(search_result.candidates)
                source_metrics = dict(search_result.metrics)
            else:
                candidates = list(search_result)
                source_metrics = {}

            if task.source == SourceName.brave_search:
                novel, google_matches = await self.store.partition_discoveries(
                    local_run_id, candidates
                )
                enrich = getattr(self.sources, "enrich_discovered", None)
                if (
                    enrich is not None
                    and SourceName.official_website in claim.snapshot.campaign.sources
                ):
                    novel = [await enrich(candidate, task) for candidate in novel]
                candidates = [*google_matches, *novel]
                source_metrics.update(
                    {
                        "google_duplicates_merged": len(google_matches),
                        "novel_brave_candidates_researched": len(novel),
                    }
                )
            qualified: list[ProspectCandidate] = []
            rejection_counts: dict[str, int] = {}
            for candidate in candidates:
                candidate = classify_and_score(candidate, claim.snapshot)
                candidate = sanitize_unsubstantiated_external_fields(candidate)
                result = validate_candidate(candidate, claim.snapshot)
                if result.accepted:
                    qualified.append(candidate)
                else:
                    for reason in result.reasons:
                        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

            limited = max(0, len(qualified) - task.max_results)
            selected = qualified[: task.max_results]
            saved = await self.store.save_candidates(local_run_id, task, selected)
            await self.store.finish_task(
                task.id,
                self.worker_id,
                results=len(saved),
                rejected=len(candidates) - len(qualified) + limited,
            )
            await self.store.save_event(
                local_run_id,
                task,
                RunEvent(
                    event_id=str(uuid.uuid4()),
                    run_id=claim.snapshot.crm_run_id,
                    task_id=task.id,
                    source=task.source,
                    keyword=task.keyword,
                    comuna_code=task.comuna_code,
                    comuna_name=task.comuna_name,
                    task_status="completed",
                    level=(
                        EventLevel.warning
                        if source_metrics.get("budget_limited")
                        else EventLevel.info
                    ),
                    stage="task_completed",
                    message=(
                        f"{task.source.value}: {task.keyword} en {task.comuna_name} completada"
                    ),
                    metrics={
                        **source_metrics,
                        "results_found": len(candidates),
                        "results_qualified": len(qualified),
                        "results_accepted": len(saved),
                        "results_discarded": len(candidates) - len(qualified),
                        "results_limited": limited,
                        "type_unconfirmed": sum(
                            "target_type_unconfirmed" in candidate.review_flags
                            for candidate in selected
                        ),
                        **rejection_counts,
                    },
                ),
            )
        except Exception as exc:  # noqa: BLE001 - retry is part of the worker contract
            error = self._safe_error(exc)
            await self.store.fail_task(task.id, self.worker_id, error)
            await self.store.save_event(
                local_run_id,
                task,
                RunEvent(
                    event_id=str(uuid.uuid4()),
                    run_id=claim.snapshot.crm_run_id,
                    task_id=task.id,
                    source=task.source,
                    keyword=task.keyword,
                    comuna_code=task.comuna_code,
                    comuna_name=task.comuna_name,
                    task_status=(
                        "failed" if task.attempt_count >= task.max_attempts else "pending"
                    ),
                    level=EventLevel.error,
                    stage="task_failed",
                    message=f"{task.source.value}: intento {task.attempt_count} falló",
                    metrics={"error_type": type(exc).__name__},
                ),
            )
        finally:
            task_heartbeat.cancel()
            await asyncio.gather(task_heartbeat, return_exceptions=True)

    async def _search_until_stopped(
        self,
        task: WorkerTask,
        claim: ClaimedRun,
        task_lease_lost: asyncio.Event,
        lease_lost: asyncio.Event,
        cancel_requested: asyncio.Event,
    ) -> SourceSearchResult | list[ProspectCandidate] | None:
        """Cancel an in-flight source at its next await when work must stop.

        Authorized connectors await between discovery, each details request,
        and each website enrichment. Cancelling this task therefore prevents
        the next quota-consuming call from being started.
        """
        source_call = asyncio.create_task(self.sources.search(task, claim.snapshot))
        stop_waiters = [
            asyncio.create_task(event.wait())
            for event in (task_lease_lost, lease_lost, cancel_requested)
        ]
        try:
            await asyncio.wait(
                [source_call, *stop_waiters],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task_lease_lost.is_set() or lease_lost.is_set() or cancel_requested.is_set():
                if not source_call.done():
                    source_call.cancel()
                await asyncio.gather(source_call, return_exceptions=True)
                return None
            return await source_call
        finally:
            for waiter in stop_waiters:
                waiter.cancel()
            await asyncio.gather(*stop_waiters, return_exceptions=True)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        message = str(exc)
        message = re.sub(
            r"(?i)(api[_-]?key|token|authorization)\s*[=:]\s*\S+", r"\1=[REDACTED]", message
        )
        return f"{type(exc).__name__}: {message}"[:1000]

    async def _deliver_outbox(self, local_run_id: str, claim: ClaimedRun) -> bool:
        while True:
            messages = await self.store.get_outbox(local_run_id)
            if not messages:
                return not await self.store.has_pending_outbox(local_run_id)

            for message in messages:
                try:
                    ack = await self._deliver_message(message, claim)
                except CRMLeaseLostError:
                    return False
                except CRMRetryableError as exc:
                    await self.store.mark_outbox_failed(
                        message.id, self._safe_error(exc), retryable=True
                    )
                    return False
                except (CRMPermanentError, ValueError) as exc:
                    safe_error = self._safe_error(exc)
                    if message.kind == "candidates" and isinstance(message.payload, list):
                        if len(message.payload) > 1:
                            if await self.store.split_candidate_outbox(message.id, safe_error):
                                logger.warning(
                                    "CRM rejected candidate batch; split for isolation size=%s",
                                    len(message.payload),
                                )
                                continue
                        elif len(message.payload) == 1:
                            if await self.store.discard_candidate_outbox(message.id, safe_error):
                                logger.warning(
                                    "CRM rejected one candidate; discarded safely error=%s",
                                    safe_error,
                                )
                                continue
                    await self.store.mark_outbox_failed(message.id, safe_error, retryable=False)
                    return False
                except CRMIdempotencyConflict as exc:
                    await self.store.mark_outbox_failed(
                        message.id, self._safe_error(exc), retryable=False
                    )
                    return False
                except Exception as exc:  # noqa: BLE001 - durable outbox retries on next claim
                    await self.store.mark_outbox_failed(
                        message.id, self._safe_error(exc), retryable=True
                    )
                    return False
                if ack is not None:
                    await self.store.record_candidate_ack(local_run_id, message.id, ack)
                await self._finalize_outbox_delivery(local_run_id, message)

    async def _reconcile_terminal_outbox(self) -> bool:
        replays = await self.store.pending_terminal_replays()
        for replay in replays:
            try:
                await self._deliver_terminal_replay(replay)
            except (CRMLeaseLostError, CRMRetryableError) as exc:
                await self.store.mark_outbox_failed(
                    replay.message.id,
                    self._safe_error(exc),
                    retryable=True,
                )
                continue
            except (CRMIdempotencyConflict, CRMPermanentError, ValueError) as exc:
                await self.store.mark_outbox_failed(
                    replay.message.id,
                    self._safe_error(exc),
                    retryable=False,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - retry on the next independent sweep
                await self.store.mark_outbox_failed(
                    replay.message.id,
                    self._safe_error(exc),
                    retryable=True,
                )
                continue
            await self._finalize_outbox_delivery(
                replay.local_run_id,
                replay.message,
            )
        return bool(replays)

    async def _deliver_terminal_replay(self, replay: TerminalOutboxReplay) -> None:
        message = replay.message
        if message.kind == "complete":
            report = CompletionReport.model_validate(message.payload)
            await self.crm.complete_run(
                replay.crm_run_id,
                replay.lease_token,
                report,
                message.idempotency_key,
                worker_id=replay.worker_id,
            )
            return
        if message.kind == "fail":
            await self.crm.fail_run(
                replay.crm_run_id,
                replay.lease_token,
                str(message.payload["error"]),
                message.idempotency_key,
                worker_id=replay.worker_id,
            )
            return
        raise ValueError(f"unsupported terminal outbox kind: {message.kind}")

    async def _finalize_outbox_delivery(self, local_run_id: str, message: OutboxEnvelope) -> None:
        if message.kind == "complete":
            report = CompletionReport.model_validate(message.payload)
            await self.store.finalize_terminal_outbox(
                local_run_id,
                message.id,
                report.status.value,
                stats=report.stats,
            )
            return
        if message.kind == "fail":
            await self.store.finalize_terminal_outbox(
                local_run_id,
                message.id,
                RunStatus.failed.value,
                error=str(message.payload["error"]),
            )
            return
        await self.store.mark_outbox_delivered(message.id)

    async def _deliver_message(
        self, message: OutboxEnvelope, claim: ClaimedRun
    ) -> CandidateBatchAck | None:
        run_id = claim.snapshot.crm_run_id
        if message.kind == "events":
            events = TypeAdapter(list[RunEvent]).validate_python(message.payload)
            await self.crm.append_events(run_id, claim.lease_token, events, message.idempotency_key)
            return None
        elif message.kind == "candidates":
            candidates = TypeAdapter(list[ProspectCandidate]).validate_python(message.payload)
            return await self.crm.upsert_candidates(
                run_id, claim.lease_token, candidates, message.idempotency_key
            )
        elif message.kind == "complete":
            report = CompletionReport.model_validate(message.payload)
            await self.crm.complete_run(run_id, claim.lease_token, report, message.idempotency_key)
            return None
        elif message.kind == "fail":
            await self.crm.fail_run(
                run_id,
                claim.lease_token,
                str(message.payload["error"]),
                message.idempotency_key,
            )
            return None
        else:
            raise ValueError(f"unsupported outbox kind: {message.kind}")

    async def _complete(
        self,
        local_run_id: str,
        claim: ClaimedRun,
        status: RunStatus,
        *,
        extra_stats: dict | None = None,
    ) -> None:
        summary = await self.store.summary(local_run_id)
        stats = {**summary.__dict__, **(extra_stats or {})}
        report = CompletionReport(status=status, stats=stats)
        key = f"{claim.snapshot.crm_run_id}:terminal:{status.value}"
        await self.store.queue_terminal(
            local_run_id, "complete", report.model_dump(mode="json"), key
        )
        await self._deliver_outbox(local_run_id, claim)

    async def _fail_run(self, local_run_id: str, claim: ClaimedRun, error: str) -> None:
        key = f"{claim.snapshot.crm_run_id}:terminal:failed"
        await self.store.queue_terminal(local_run_id, "fail", {"error": error}, key)
        await self._deliver_outbox(local_run_id, claim)

    async def _cancel(self, local_run_id: str, claim: ClaimedRun) -> None:
        await self.store.cancel_tasks(local_run_id)
        await self.store.save_event(
            local_run_id,
            None,
            RunEvent(
                event_id=str(uuid.uuid4()),
                run_id=claim.snapshot.crm_run_id,
                stage="run_cancelled",
                message="Ejecución cancelada por el CRM",
            ),
        )
        await self._complete(local_run_id, claim, RunStatus.cancelled)
