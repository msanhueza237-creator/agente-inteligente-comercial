from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from app.config import get_settings
from app.hub.agents import AgentRegistry
from app.hub.crm import HubCRMPort
from app.hub.inventory import extract_product_snapshots, payload_rows
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
    await run_hub_services(crm)


async def run_hub_services(crm: HubCRMPort) -> None:
    """Run the queue consumer and integration monitor as one supervised unit."""

    settings = get_settings()
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
                    raw_products = (
                        await load_paginated_records(client.products, row_keys=("products", "items"))
                        if provider == "facto"
                        else await client.products(page=1)
                    )
                    snapshot_products = raw_products
                    records = normalize_product_records(raw_products)
                    await crm.upsert_integration_records(
                        provider=provider,
                        resource="products",
                        records=records,
                    )
                    # Facto is the ERP source of truth.  Its documents are
                    # read only and are used only to calculate an auditable
                    # sales-rate snapshot for the foreign-trade agent.
                    if provider == "facto":
                        raw_documents = {}
                        snapshot_documents = raw_documents
                        try:
                            raw_documents = await client.documents(page=1)
                            snapshot_documents = raw_documents
                            await crm.upsert_integration_records(
                                provider="facto",
                                resource="documents",
                                records=normalize_product_records(raw_documents),
                            )
                            snapshot_products, snapshot_documents = await load_facto_details(
                                client,
                                raw_products,
                                raw_documents,
                            )
                            detail_product_records = normalize_product_records(snapshot_products)
                            await crm.upsert_integration_records(
                                provider="facto",
                                resource="product_details",
                                records=detail_product_records,
                            )
                            document_records = normalize_product_records(snapshot_documents)
                            await crm.upsert_integration_records(
                                provider="facto",
                                resource="document_details",
                                records=document_records,
                            )
                        except Exception:  # noqa: BLE001
                            # Product synchronization remains healthy when a
                            # Facto account does not expose sales documents.
                            logger.warning("Facto documents are not available for inventory analysis", exc_info=True)
                        # Always persist a product snapshot.  If documents are
                        # unavailable it is marked as insufficient for a
                        # purchase recommendation, instead of disappearing
                        # from the CRM completely.
                        snapshots = extract_product_snapshots(
                            snapshot_products,
                            snapshot_documents,
                        )
                        await crm.upsert_integration_records(
                            provider="facto",
                            resource="inventory_snapshots",
                            records=snapshots,
                        )
            except Exception:  # noqa: BLE001
                logger.exception("integration status failed provider=%s", provider)
        await asyncio.sleep(min(
            settings.facto_sync_interval_minutes,
            settings.tiendanube_sync_interval_minutes,
        ) * 60)


async def load_facto_details(
    client: FactoClient,
    products_payload,
    documents_payload,
    *,
    concurrency: int = 5,
) -> tuple[list[dict], list[dict]]:
    """Read full Facto records required for stock and auditable sales demand."""

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def product_detail(row: dict) -> dict:
        product_id = row.get("product_id") or row.get("id")
        if product_id is None:
            return row
        async with semaphore:
            try:
                detail = await client.product(product_id)
            except Exception:  # noqa: BLE001
                logger.warning("Facto product detail unavailable id=%s", product_id)
                return row
        return detail if isinstance(detail, dict) else row

    async def document_detail(row: dict) -> dict:
        document_id = row.get("document_id") or row.get("id")
        if document_id is None:
            return row
        async with semaphore:
            try:
                detail = await client.document(document_id)
            except Exception:  # noqa: BLE001
                logger.warning("Facto document detail unavailable id=%s", document_id)
                return row
        return detail if isinstance(detail, dict) else row

    product_rows = payload_rows(products_payload, "data", "products", "items")
    document_rows = payload_rows(documents_payload, "data", "documents", "items")
    products = await asyncio.gather(*(product_detail(row) for row in product_rows))
    documents = await asyncio.gather(*(document_detail(row) for row in document_rows[:100]))
    return list(products), list(documents)


async def load_paginated_records(
    page_loader,
    *,
    row_keys: tuple[str, ...] = ("data", "items"),
    max_pages: int = 200,
) -> list[dict]:
    """Load every provider page without trusting one specific pagination envelope.

    Facto installations do not all expose the same metadata.  The worker
    therefore advances until an empty page, a repeated page, an explicit last
    page or a short page after the first response.  A hard upper bound protects
    the ERP from an accidental infinite loop.
    """

    collected: list[dict] = []
    fingerprints: set[str] = set()
    expected_page_size: int | None = None

    for page in range(1, max_pages + 1):
        payload = await page_loader(page=page)
        rows = payload_rows(payload, *row_keys)
        if not rows:
            break

        fingerprint = hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=str).encode()
        ).hexdigest()
        if fingerprint in fingerprints:
            logger.warning("provider repeated page=%s; pagination stopped safely", page)
            break
        fingerprints.add(fingerprint)
        collected.extend(rows)

        if expected_page_size is None:
            expected_page_size = len(rows)
        if _is_explicit_last_page(payload, page):
            break
        if expected_page_size and len(rows) < expected_page_size:
            break
    else:
        logger.warning("provider pagination reached safety limit pages=%s", max_pages)

    return collected


def _is_explicit_last_page(payload, page: int) -> bool:
    if not isinstance(payload, dict):
        return False
    candidates = [payload]
    for key in ("pagination", "meta", "paging"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for data in candidates:
        last_page = (
            data.get("last_page")
            or data.get("lastPage")
            or data.get("total_pages")
            or data.get("totalPages")
            or data.get("pages")
        )
        try:
            if last_page is not None and page >= int(last_page):
                return True
        except (TypeError, ValueError):
            pass
        if data.get("next") is None and any(
            key in data for key in ("next", "next_page", "nextPage")
        ):
            return True
    return False


def normalize_product_records(payload) -> list[dict]:
    rows = payload_rows(payload, "data", "products", "documents", "items")
    result: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        external_id = (
            row.get("id")
            or row.get("product_id")
            or row.get("document_id")
            or row.get("sku")
            or row.get("code")
        )
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
