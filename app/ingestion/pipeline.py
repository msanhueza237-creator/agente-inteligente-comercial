"""Orchestrates one Excel/CSV import: parse -> auto-map columns -> normalize
-> dedup match -> create/update prospects, with a full audit trail
(import_batches, research_jobs, prospect_sources, dedup_candidates).

This is the v1 (Fase 1) version: column mapping is auto-suggested and
applied directly rather than staged for human confirmation first -- that
review step is a natural fast-follow once real client files have been
tried against this pipeline.
"""

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.classification.rules import classify_category_from_text
from app.db.models import ImportBatch, ImportBatchStatus, JobStatus, JobType, ResearchJob, SourceType
from app.dedup.pool import load_existing_pool, resolve_prospect
from app.ingestion.column_mapper import suggest_column_mapping
from app.ingestion.excel_parser import read_table, rows_from_mapping
from app.normalization.address import normalize_address
from app.normalization.name import normalize_name
from app.normalization.phone import normalize_phone
from app.normalization.rut import normalize_rut
from app.normalization.website import normalize_website


def _prospect_fields(row: dict) -> dict:
    """Raw values destined for storage on the Prospect row."""
    _, legal_form = normalize_name(row.get("name"))
    return {
        "name": row.get("name"),
        "legal_form": legal_form,
        "trade_name": row.get("trade_name"),
        "rut": normalize_rut(row.get("rut")),
        "region": row.get("region"),
        "comuna": row.get("comuna"),
        "city": row.get("city"),
        "address": row.get("address"),
        "address_normalized": normalize_address(row.get("address")),
        "phone": normalize_phone(row.get("phone")),
        "phone_raw": row.get("phone"),
        "email": row.get("email"),
        "website": normalize_website(row.get("website")),
        "notes": row.get("notes"),
        "category": classify_category_from_text(row.get("category"), row.get("notes")),
        "google_place_id": None,
    }


async def ingest_excel(
    session: AsyncSession,
    *,
    content: bytes,
    filename: str,
    uploaded_by: str | None = None,
) -> ImportBatch:
    df = read_table(content, filename)
    mapping = suggest_column_mapping(list(df.columns))
    rows = rows_from_mapping(df, mapping)

    batch = ImportBatch(
        filename=filename,
        uploaded_by=uploaded_by,
        column_mapping=mapping,
        row_count=len(rows),
        status=ImportBatchStatus.processing,
    )
    session.add(batch)
    await session.flush()

    job = ResearchJob(
        job_type=JobType.excel_import,
        parameters={"import_batch_id": str(batch.id), "filename": filename},
        status=JobStatus.running,
        started_at=datetime.now(timezone.utc),
        triggered_by=uploaded_by,
    )
    session.add(job)
    await session.flush()

    stats = {"created": 0, "merged": 0, "needs_review": 0, "errors": 0}
    existing_pool = await load_existing_pool(session)

    for raw_row in rows:
        try:
            fields = _prospect_fields(raw_row)
            if not fields.get("name"):
                stats["errors"] += 1
                continue

            await resolve_prospect(
                session,
                fields,
                source_type=SourceType.excel_import,
                job_id=job.id,
                existing_pool=existing_pool,
                stats=stats,
                raw_source_data=raw_row,
                fetched_by=uploaded_by,
            )
        except Exception as exc:  # noqa: BLE001 -- one bad row must not abort the batch
            stats["errors"] += 1
            job.error_log = f"{job.error_log or ''}\nFila con error: {raw_row!r} -> {exc}"

    job.status = JobStatus.completed if stats["errors"] == 0 else JobStatus.partial
    job.finished_at = datetime.now(timezone.utc)
    job.stats = stats

    batch.status = ImportBatchStatus.completed
    batch.row_count = len(rows)

    await session.commit()
    return batch
