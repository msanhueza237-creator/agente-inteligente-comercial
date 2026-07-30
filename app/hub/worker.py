from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date

from app.config import get_settings
from app.hub.agents import AgentRegistry
from app.hub.crm import HubCRMPort
from app.hub.finance import extract_financial_snapshot
from app.hub.inventory import extract_product_snapshots, payload_rows
from app.integrations.facto import FactoClient
from app.integrations.tiendanube import TiendanubeClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clima_activa.hub")

# Facto detail responses are stable after issuance. Keep them in memory so the
# 30-minute monitor only requests new documents after the first full sync.
_facto_document_detail_cache: dict[str, dict] = {}

# Facto Chile: emitted sales documents and received purchase documents.
# Dispatch guides and customs DIN records are excluded from financial totals
# because they do not represent a net sale or purchase by themselves.
FACTO_SALES_DOCUMENT_TYPE_IDS = {2, 32, 37, 39, 41, 46}
FACTO_PURCHASE_DOCUMENT_TYPE_IDS = {9, 15, 28, 30, 33, 34, 38, 40, 42}


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
                        raw_documents: list[dict] = []
                        raw_purchase_documents: list[dict] = []
                        raw_payments: list[dict] | None = None
                        snapshot_documents: list[dict] = []
                        snapshot_purchase_documents: list[dict] = []
                        try:
                            raw_documents, raw_purchase_documents = (
                                await load_facto_financial_documents(client)
                            )
                            try:
                                inbox_documents = await load_facto_inbox_documents(client)
                                raw_purchase_documents = enrich_facto_purchase_documents(
                                    raw_purchase_documents,
                                    inbox_documents,
                                )
                            except Exception:  # noqa: BLE001
                                # /inbox_documents enriches the supplier name,
                                # but purchase totals and RUTs remain usable if
                                # an older Facto installation lacks this route.
                                logger.warning(
                                    "Facto inbox metadata is not available for supplier names",
                                    exc_info=True,
                                )
                            snapshot_documents = raw_documents
                            snapshot_purchase_documents = raw_purchase_documents
                            await crm.upsert_integration_records(
                                provider="facto",
                                resource="documents",
                                records=normalize_product_records(raw_documents),
                            )
                            await crm.upsert_integration_records(
                                provider="facto",
                                resource="purchase_documents",
                                records=normalize_product_records(raw_purchase_documents),
                            )
                            try:
                                raw_payments = await load_facto_payments(client)
                                await crm.upsert_integration_records(
                                    provider="facto",
                                    resource="payments",
                                    records=normalize_product_records(raw_payments),
                                )
                            except Exception:  # noqa: BLE001
                                # The public Facto reference does not guarantee
                                # GET /payments for every account. Finance will
                                # expose documentary credit instead of claiming
                                # an unpaid balance when this collection is absent.
                                raw_payments = None
                                logger.info(
                                    "Facto payment collection is not available; using documentary credit exposure"
                                )
                            snapshot_products, detailed_documents = await load_facto_details(
                                client,
                                raw_products,
                                [*raw_documents, *raw_purchase_documents],
                            )
                            snapshot_documents = [
                                row for row in detailed_documents if _is_facto_sales_document(row)
                            ]
                            snapshot_purchase_documents = [
                                row for row in detailed_documents if _is_facto_purchase_document(row)
                            ]
                            detail_product_records = normalize_product_records(snapshot_products)
                            await crm.upsert_integration_records(
                                provider="facto",
                                resource="product_details",
                                records=detail_product_records,
                            )
                            await crm.upsert_integration_records(
                                provider="facto",
                                resource="document_details",
                                records=normalize_product_records(snapshot_documents),
                            )
                            await crm.upsert_integration_records(
                                provider="facto",
                                resource="purchase_document_details",
                                records=normalize_product_records(snapshot_purchase_documents),
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
                        financial_snapshots = extract_financial_snapshot(
                            snapshot_documents,
                            snapshots,
                            purchase_documents_payload=snapshot_purchase_documents,
                            payments_payload=raw_payments,
                        )
                        await crm.upsert_integration_records(
                            provider="facto",
                            resource="financial_snapshots",
                            records=financial_snapshots,
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
        return {**row, **detail} if isinstance(detail, dict) else row

    async def document_detail(row: dict) -> dict:
        document_id = row.get("document_id") or row.get("id")
        if document_id is None:
            return row
        cache_key = str(document_id)
        if cache_key in _facto_document_detail_cache:
            return {**row, **_facto_document_detail_cache[cache_key]}
        async with semaphore:
            try:
                detail = await client.document(document_id)
            except Exception:  # noqa: BLE001
                logger.warning("Facto document detail unavailable id=%s", document_id)
                return row
        result = {**row, **detail} if isinstance(detail, dict) else row
        if isinstance(result, dict):
            _facto_document_detail_cache[cache_key] = result
        return result

    product_rows = payload_rows(products_payload, "data", "products", "items")
    document_rows = payload_rows(documents_payload, "data", "documents", "items")
    products = await asyncio.gather(*(product_detail(row) for row in product_rows))
    documents = await asyncio.gather(*(document_detail(row) for row in document_rows))
    return list(products), list(documents)


def _facto_document_type_id(row: dict) -> int | None:
    value = (
        row.get("document_type_id")
        or row.get("type_id")
        or row.get("document_type")
    )
    if isinstance(value, dict):
        value = value.get("document_type_id") or value.get("id")
    if value is None and isinstance(row.get("header"), dict):
        return _facto_document_type_id(row["header"])
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_facto_sales_document(row: dict) -> bool:
    """Keep emitted invoices/receipts and exclude credits and dispatch guides."""

    return _facto_document_type_id(row) in FACTO_SALES_DOCUMENT_TYPE_IDS


def _is_facto_purchase_document(row: dict) -> bool:
    """Keep received purchases, including received credit and debit notes."""

    return _facto_document_type_id(row) in FACTO_PURCHASE_DOCUMENT_TYPE_IDS


async def load_facto_financial_documents(
    client: FactoClient,
    *,
    history_start: date | None = None,
    max_pages: int = 20,
) -> tuple[list[dict], list[dict]]:
    """Load and partition Facto documents into issued sales and purchases."""

    end = date.today()
    # The finance dashboard compares the current year with all of 2025.
    # Keep this baseline explicit so it does not silently move every day.
    start = history_start or date(2025, 1, 1)
    sales: list[dict] = []
    purchases: list[dict] = []
    for page in range(1, max_pages + 1):
        payload = await client.documents(
            page=page,
            per_page=100,
            issue_date_from=start.isoformat(),
            issue_date_to=end.isoformat(),
            order_by="desc",
            document_status=1,
        )
        rows = payload_rows(payload, "documents", "items")
        sales.extend(row for row in rows if _is_facto_sales_document(row))
        purchases.extend(row for row in rows if _is_facto_purchase_document(row))
        if not rows or _is_explicit_last_page(payload, page):
            break
    else:
        logger.warning("Facto finance pagination reached safety limit pages=%s", max_pages)
    return sales, purchases


async def load_facto_inbox_documents(
    client: FactoClient,
    *,
    max_pages: int = 50,
) -> list[dict]:
    """Load received-document metadata used to identify purchase suppliers."""

    rows: list[dict] = []
    fingerprints: set[str] = set()
    expected_page_size: int | None = None
    for page in range(1, max_pages + 1):
        payload = await client.inbox_documents(page=page, per_page=100)
        page_rows = payload_rows(
            payload,
            "inbox_documents",
            "documents",
            "items",
        )
        if not page_rows:
            break
        fingerprint = hashlib.sha256(
            json.dumps(page_rows, sort_keys=True, default=str).encode()
        ).hexdigest()
        if fingerprint in fingerprints:
            break
        fingerprints.add(fingerprint)
        rows.extend(page_rows)
        if expected_page_size is None:
            expected_page_size = len(page_rows)
        if _is_explicit_last_page(payload, page):
            break
        if expected_page_size and len(page_rows) < expected_page_size:
            break
    else:
        logger.warning("Facto inbox pagination reached safety limit pages=%s", max_pages)
    return rows


async def load_facto_payments(
    client: FactoClient,
    *,
    max_pages: int = 50,
) -> list[dict]:
    """Feature-detect and paginate the optional Facto payment collection."""

    rows: list[dict] = []
    fingerprints: set[str] = set()
    for page in range(1, max_pages + 1):
        payload = await client.payments(page=page, per_page=100)
        page_rows = payload_rows(payload, "payments", "items")
        if not page_rows:
            break
        fingerprint = hashlib.sha256(
            json.dumps(page_rows, sort_keys=True, default=str).encode()
        ).hexdigest()
        if fingerprint in fingerprints:
            break
        fingerprints.add(fingerprint)
        rows.extend(page_rows)
        if _is_explicit_last_page(payload, page) or len(page_rows) < 100:
            break
    else:
        logger.warning("Facto payment pagination reached safety limit pages=%s", max_pages)
    return rows


def enrich_facto_purchase_documents(
    purchase_documents: list[dict],
    inbox_documents: list[dict],
) -> list[dict]:
    """Attach issuer name/RUT from Facto's received-document inbox."""

    by_document_id = {
        str(row.get("document_id")): row
        for row in inbox_documents
        if row.get("document_id") not in (None, "")
    }
    enriched: list[dict] = []
    for purchase in purchase_documents:
        match = by_document_id.get(str(purchase.get("document_id")))
        if not match:
            enriched.append(purchase)
            continue
        supplier_metadata = {
            key: match.get(key)
            for key in (
                "issuer_name",
                "issuer_tax_id_code",
                "issuer_tax_id_type",
                "sender_email",
                "purchase_order",
                "receive_date",
                "product_receipt_date",
                "product_receipt_location",
            )
            if match.get(key) not in (None, "")
        }
        enriched.append({**purchase, **supplier_metadata})
    return enriched


async def load_facto_sales_documents(
    client: FactoClient,
    *,
    history_start: date | None = None,
    max_pages: int = 20,
) -> list[dict]:
    """Load issued Facto sales documents from the auditable finance baseline."""

    sales, _ = await load_facto_financial_documents(
        client,
        history_start=history_start,
        max_pages=max_pages,
    )
    return sales


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
            or data.get("page_count")
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
