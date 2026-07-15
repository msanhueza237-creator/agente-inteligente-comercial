"""Persistent prospecting worker entrypoint."""

import asyncio
import logging

from app.config import get_settings
from app.crm.fake import FakeCRMPort
from app.crm.http import HttpCRMPort
from app.crm.port import CRMPort
from app.db.base import async_session_factory
from app.integrations.monitor import IntegrationMonitor
from app.prospecting.budget import PersistentGooglePlacesBudget
from app.prospecting.sources import AuthorizedSourceExecutor
from app.prospecting.retention import purge_expired_source_data
from app.prospecting.store import SQLWorkerStore
from app.prospecting.worker import ProspectingWorker, WorkerConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clima_activa.worker")


def build_crm_port(settings) -> CRMPort:
    if settings.crm_mode == "http":
        return HttpCRMPort(
            base_url=settings.crm_base_url,
            api_key=settings.crm_api_key.get_secret_value(),
            timeout=settings.crm_timeout_seconds,
        )
    return FakeCRMPort()


def build_worker() -> ProspectingWorker:
    settings = get_settings()
    google_budget = PersistentGooglePlacesBudget(settings, async_session_factory)
    return ProspectingWorker(
        crm=build_crm_port(settings),
        store=SQLWorkerStore(async_session_factory),
        sources=AuthorizedSourceExecutor(budget=google_budget),
        worker_id=settings.crm_worker_id,
        config=WorkerConfig(
            poll_seconds=settings.worker_poll_seconds,
            lease_seconds=settings.worker_lease_seconds,
            heartbeat_seconds=settings.worker_heartbeat_seconds,
            task_max_attempts=settings.worker_task_max_attempts,
        ),
    )


async def main() -> None:
    worker = build_worker()
    settings = get_settings()
    logger.info(
        "prospecting worker started mode=%s worker_id=%s",
        settings.crm_mode,
        settings.crm_worker_id,
    )
    retention_task = asyncio.create_task(_retention_loop())
    integration_task = None
    if isinstance(worker.crm, HttpCRMPort):
        integration_task = asyncio.create_task(_integration_loop(worker.crm, settings))
    try:
        await worker.run_forever()
    finally:
        retention_task.cancel()
        if integration_task:
            integration_task.cancel()
        await asyncio.gather(
            retention_task,
            *([integration_task] if integration_task else []),
            return_exceptions=True,
        )


async def _integration_loop(crm: HttpCRMPort, settings) -> None:
    monitor = IntegrationMonitor(crm, settings)
    while True:
        try:
            await monitor.poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an integration test must not stop the worker
            logger.exception("integration check failed")
        await asyncio.sleep(settings.worker_poll_seconds)


async def _retention_loop() -> None:
    while True:
        try:
            async with async_session_factory() as session, session.begin():
                stats = await purge_expired_source_data(session)
            if any(stats.values()):
                logger.info("expired prospecting evidence purged: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - maintenance must not stop discovery
            logger.exception("source evidence retention purge failed")
        await asyncio.sleep(6 * 60 * 60)


if __name__ == "__main__":
    asyncio.run(main())
