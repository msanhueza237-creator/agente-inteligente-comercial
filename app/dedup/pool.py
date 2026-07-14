"""Shared helpers for building dedup-matching candidates and loading the
pool of existing prospects to match against. Used by both the Excel/CSV
ingestion pipeline and the web-search enrichment pipeline so the two stay
consistent about what "the same prospect" means.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from unidecode import unidecode

from app.db.models import (
    DedupCandidate,
    DedupStatus,
    Prospect,
    ProspectSource,
    ProspectStatus,
    SourceType,
)
from app.dedup.matcher import match
from app.dedup.merge import merge_fields
from app.normalization.name import normalize_name


def clean_text(value: str | None) -> str | None:
    return unidecode(value).upper().strip() if value else None


def build_match_candidate(fields: dict) -> dict:
    """Project a Prospect-shaped field dict down to the subset used purely
    for dedup comparison (see app.dedup.matcher)."""
    name_normalized, _ = normalize_name(fields.get("name"))
    return {
        "google_place_id": fields.get("google_place_id"),
        "rut": fields.get("rut"),
        "website": fields.get("website"),
        "phone": fields.get("phone"),
        "name": name_normalized,
        "address_normalized": fields.get("address_normalized"),
        "comuna": clean_text(fields.get("comuna")),
    }


async def load_existing_pool(session: AsyncSession) -> list[dict]:
    """Existing prospects available as dedup match targets. Loads everything
    not already merged away; filtering by region/category is a future
    optimization once volume warrants it (see plan Fase 2)."""
    result = await session.execute(
        select(Prospect).where(Prospect.dedup_status != DedupStatus.merged)
    )
    prospects = result.scalars().all()

    pool = []
    for p in prospects:
        name_normalized, _ = normalize_name(p.name)
        pool.append(
            {
                "id": p.id,
                "google_place_id": p.google_place_id,
                "rut": p.rut,
                "website": p.website,
                "phone": p.phone,
                "name": name_normalized,
                "address_normalized": p.address_normalized,
                "comuna": clean_text(p.comuna),
            }
        )
    return pool


async def resolve_prospect(
    session: AsyncSession,
    fields: dict,
    *,
    source_type: SourceType,
    job_id,
    existing_pool: list[dict],
    stats: dict,
    raw_source_data: dict | None = None,
    source_url: str | None = None,
    fetched_by: str | None = None,
) -> Prospect:
    """Run one incoming record (from Excel or from a web search hit) through
    dedup matching, create/merge as needed, record the ProspectSource audit
    row, and update `stats` in place ({"created","merged","needs_review",...}).
    Shared by the ingestion pipeline and the enrichment pipeline so "same
    prospect" is decided identically regardless of where the data came from.
    """
    candidate = build_match_candidate(fields)
    result = match(candidate, existing_pool)

    if result.decision == "auto_merge":
        target = await session.get(Prospect, result.matched_id)
        existing_dict = {c.name: getattr(target, c.name) for c in Prospect.__table__.columns}
        merged, conflicts = merge_fields(
            existing_dict,
            fields,
            existing_source=source_type.value,
            incoming_source=source_type.value,
        )
        if not conflicts:
            for field, value in merged.items():
                if field in fields:
                    setattr(target, field, value)
            stats["merged"] += 1
            prospect = target
        else:
            prospect = Prospect(**fields, status=ProspectStatus.new, dedup_status=DedupStatus.needs_review)
            session.add(prospect)
            await session.flush()
            session.add(
                DedupCandidate(
                    prospect_a_id=target.id,
                    prospect_b_id=prospect.id,
                    match_score=result.score,
                    match_reasons=result.reasons,
                )
            )
            stats["needs_review"] += 1
    elif result.decision == "needs_review":
        prospect = Prospect(**fields, status=ProspectStatus.new, dedup_status=DedupStatus.needs_review)
        session.add(prospect)
        await session.flush()
        session.add(
            DedupCandidate(
                prospect_a_id=result.matched_id,
                prospect_b_id=prospect.id,
                match_score=result.score,
                match_reasons=result.reasons,
            )
        )
        stats["needs_review"] += 1
    else:
        prospect = Prospect(**fields, status=ProspectStatus.new, dedup_status=DedupStatus.unique)
        session.add(prospect)
        await session.flush()
        existing_pool.append(candidate | {"id": prospect.id})
        stats["created"] += 1

    session.add(
        ProspectSource(
            prospect_id=prospect.id,
            job_id=job_id,
            source_type=source_type,
            source_url=source_url,
            raw_data=raw_source_data,
            fetched_by=fetched_by,
        )
    )
    return prospect
