from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from app.config import get_settings
from app.hub.agents import AgentRegistry
from app.hub.crm import HubCRMPort
from app.integrations.facto import FactoClient
from app.integrations.tiendanube import TiendanubeClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clima_activa.hub")


class AgentHubWorker:
    def __init__(self, crm: HubCRMPort, registry: AgentRegistry) -> None:
        self.settings = get_settings()
        self.crm = crm
        self.registry = registry

    async def run_forever(self) -> None:
        while True:
            try:
                claimed = await self.crm.claim(
                    self.settings.hub_worker_id,
                    lease_seconds=self.settings.hub_lease_seconds,
                )
                if claimed is None:
                    await asyncio.sleep(self.settings.hub_poll_seconds)
                    continue
                heartbeat = asyncio.create_task(self._heartbeat(claimed))
                try:
                    result = await self.registry.get(claimed.agent_type).execute(claimed)
                    await self.crm.complete(claimed, self.settings.hub_worker_id, result)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("agent task failed task_id=%s", claimed.id)
                    await self.crm.fail(
                        claimed,
                        self.settings.hub_worker_id,
                        f"agent_execution_{type(exc).__name__}",
                    )
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("hub polling failed")
                await asyncio.sleep(self.settings.hub_poll_seconds)

    async def _heartbeat(self, task) -> None:
        while True:
            await asyncio.sleep(self.settings.hub_heartbeat_seconds)
            await self.crm.heartbeat(
                task,
                self.settings.hub_worker_id,
                lease_seconds=self.settings.hub_lease_seconds,
            )


async def main() -> None:
    settings = get_settings()
    if settings.crm_mode != "http":
        logger.warning("Agent Hub waits for CRM_MODE=http; no fake tasks will be executed")
        while True:
            await asyncio.sleep(60)
    crm = HubCRMPort(
        base_url=settings.crm_base_url,
        api_key=settings.crm_api_key.get_secret_value(),
        timeout=settings.crm_timeout_seconds,
    )
    logger.info(
        "agent hub started worker_id=%s agents=%s",
        settings.hub_worker_id,
        ",".join(AgentRegistry().names()),
    )
    integration_task = asyncio.create_task(integration_monitor(crm))
    try:
        await AgentHubWorker(crm, AgentRegistry()).run_forever()
    finally:
        integration_task.cancel()
        await asyncio.gather(integration_task, return_exceptions=True)


async def integration_monitor(crm: HubCRMPort) -> None:
    settings = get_settings()
    while True:
        for provider, client, enabled in (
            ("facto", FactoClient(settings), settings.facto_enabled),
            ("tiendanube", TiendanubeClient(settings), settings.tiendanube_enabled),
        ):
            try:
                health = await client.health()
                await crm.report_integration(
                    provider=provider,
                    enabled=enabled,
                    read_only=True,
                    status=(
                        "connected"
                        if health.connected
                        else "error"
                        if health.configured
                        else "pending_configuration"
                    ),
                    message=health.message,
                )
                if health.connected:
                    raw_products = await client.products(page=1)
                    records = normalize_product_records(raw_products)
                    await crm.upsert_integration_records(
                        provider=provider,
                        resource="products",
                        records=records,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("integration status failed provider=%s", provider)
        await asyncio.sleep(min(
            settings.facto_sync_interval_minutes,
            settings.tiendanube_sync_interval_minutes,
        ) * 60)


def normalize_product_records(payload) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next(
            (
                value
                for key in ("data", "products", "items")
                if isinstance((value := payload.get(key)), list)
            ),
            [],
        )
    else:
        rows = []
    result: list[dict] = []
    for index, row in enumerate(rows[:100]):
        if not isinstance(row, dict):
            continue
        external_id = row.get("id") or row.get("sku") or row.get("code")
        if external_id is None:
            external_id = hashlib.sha256(
                json.dumps(row, sort_keys=True, default=str).encode()
            ).hexdigest()[:24]
        # Product catalog data only. Customer/order resources are intentionally
        # not synchronized in the first read-only milestone.
        result.append({"external_id": str(external_id), "payload": row, "position": index})
    return result


if __name__ == "__main__":
    asyncio.run(main())
