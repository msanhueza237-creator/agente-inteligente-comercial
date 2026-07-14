"""Legacy synchronous search kept for backwards-compatible imports.

Paginas Amarillas is disabled unless both explicit licence flags are set;
new CRM-controlled runs use app.prospecting.worker instead.
enrich each hit from its own website, then dedup-match and save exactly
like the Excel ingestion pipeline does. Used by both the scheduler (Fase 6,
not yet wired up) and the dashboard's manual "buscar instaladores en
Antofagasta" trigger (Fase 1 delivers the manual path first).

Sources are independent and best-effort: if Google Places fails (e.g. no
API key configured) the search still runs against Paginas Amarillas, and
vice versa. The job is only marked `failed` if every source failed to run
at all.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.classification.rules import classify_category_from_text
from app.config import get_settings
from app.db.models import (
    GoogleMapsQueryLog,
    JobStatus,
    JobType,
    PlacesFieldTier,
    PlacesQueryType,
    ProspectSource,
    ResearchJob,
    SourceType,
)
from app.dedup.pool import load_existing_pool, resolve_prospect
from app.enrichment import paginas_amarillas
from app.enrichment.google_places import (
    COST_ESTIMATE_USD,
    GooglePlacesClient,
    GooglePlacesError,
    extract_region_comuna,
)
from app.enrichment.web_scraper import enrich_from_website
from app.normalization.address import normalize_address
from app.normalization.name import normalize_name
from app.normalization.phone import normalize_phone
from app.normalization.website import normalize_website

# Business types that indicate the result is off-topic even though the
# search query matched -- a light sanity check, not a strict filter (the
# targeted query already does most of the work).
_REJECT_TYPES = {"lodging", "restaurant", "bar", "night_club", "school"}


async def _period_spend(session: AsyncSession, *, since: datetime) -> float:
    result = await session.execute(
        select(func.coalesce(func.sum(GoogleMapsQueryLog.cost_estimate_usd), 0)).where(
            GoogleMapsQueryLog.created_at >= since
        )
    )
    return float(result.scalar_one())


async def _log_query(
    session: AsyncSession,
    *,
    query_type: PlacesQueryType,
    region: str,
    category: str,
    results_count: int,
    tier: str,
) -> None:
    session.add(
        GoogleMapsQueryLog(
            query_type=query_type,
            query_params={"region": region, "category": category},
            region=region,
            category=category,
            results_count=results_count,
            field_mask_tier=PlacesFieldTier(tier),
            cost_estimate_usd=COST_ESTIMATE_USD[tier],
        )
    )


def _looks_relevant(place: dict) -> bool:
    return not (set(place.get("types", []) or []) & _REJECT_TYPES)


def _fields_from_place(place: dict, details: dict | None) -> dict:
    source = details or place
    display_name = source.get("displayName", {}).get("text") or place.get("displayName", {}).get(
        "text"
    )
    _, legal_form = normalize_name(display_name)
    region, comuna = extract_region_comuna(source.get("addressComponents"))

    return {
        "name": display_name,
        "legal_form": legal_form,
        "trade_name": None,
        "rut": None,
        "region": region,
        "comuna": comuna,
        "city": None,
        "address": source.get("formattedAddress"),
        "address_normalized": normalize_address(source.get("formattedAddress")),
        "phone": normalize_phone(
            source.get("nationalPhoneNumber") or source.get("internationalPhoneNumber")
        ),
        "phone_raw": source.get("nationalPhoneNumber"),
        "email": None,
        "website": normalize_website(source.get("websiteUri")),
        "notes": None,
        "category": classify_category_from_text(
            display_name, " ".join(source.get("types", []) or [])
        ),
        "google_place_id": source.get("id"),
        "google_rating": source.get("rating"),
        "google_ratings_total": source.get("userRatingCount"),
        "google_maps_url": source.get("googleMapsUri"),
    }


def _fields_from_paginas_amarillas(listing: dict, detail: dict | None) -> dict:
    name = (detail or {}).get("name") or listing.get("name")
    _, legal_form = normalize_name(name)

    if detail:
        region, comuna, address = detail.get("region"), detail.get("comuna"), detail.get("address")
        phone_raw, website_raw, email = (
            detail.get("phone"),
            detail.get("website"),
            detail.get("email"),
        )
        keywords_text = " ".join(detail.get("keywords") or [])
        description = detail.get("description")
    else:
        # Fell back to listing-page-only data: "address" there is just the
        # comuna name (see paginas_amarillas.py), not a street address.
        region, comuna, address = None, listing.get("address"), None
        phone_raw, website_raw, email = listing.get("telephone"), None, listing.get("email")
        keywords_text, description = "", None

    return {
        "name": name,
        "legal_form": legal_form,
        "trade_name": None,
        "rut": None,
        "region": region,
        "comuna": comuna,
        "city": None,
        "address": address,
        "address_normalized": normalize_address(address),
        "phone": normalize_phone(phone_raw),
        "phone_raw": phone_raw,
        "email": email,
        "website": normalize_website(website_raw),
        "notes": description,
        "category": classify_category_from_text(name, keywords_text, description),
        "google_place_id": None,
    }


async def _run_google_places(
    session: AsyncSession,
    *,
    query: str,
    region: str,
    keyword: str,
    max_results: int,
    job_id,
    existing_pool: list[dict],
    stats: dict,
    triggered_by: str | None,
) -> None:
    settings = get_settings()
    client = GooglePlacesClient()  # raises GooglePlacesError if no API key -- caller catches it
    places = await client.text_search(query, max_results=max_results)

    await _log_query(
        session,
        query_type=PlacesQueryType.text_search,
        region=region,
        category=keyword,
        results_count=len(places),
        tier="pro",
    )
    stats["found_google"] = len(places)

    now = datetime.now(timezone.utc)
    daily_spend = await _period_spend(
        session, since=now.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    monthly_spend = await _period_spend(
        session, since=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    )
    enterprise_cost = COST_ESTIMATE_USD["enterprise"]

    for place in places:
        try:
            if not _looks_relevant(place):
                continue

            place_id = place.get("id")
            details = None
            budget_ok = (
                daily_spend + enterprise_cost <= settings.google_places_daily_budget_usd
                and monthly_spend + enterprise_cost <= settings.google_places_monthly_budget_usd
            )
            if place_id and budget_ok:
                details = await client.get_place_details(place_id)
                await _log_query(
                    session,
                    query_type=PlacesQueryType.place_details,
                    region=region,
                    category=keyword,
                    results_count=1 if details else 0,
                    tier="enterprise",
                )
                daily_spend += enterprise_cost
                monthly_spend += enterprise_cost
            elif place_id:
                stats["skipped_budget"] += 1

            fields = _fields_from_place(place, details)
            if not fields.get("name"):
                stats["errors"] += 1
                continue

            website_info = await enrich_from_website(fields.get("website"))
            if website_info.get("email"):
                fields["email"] = website_info["email"]
            if website_info.get("social_media"):
                fields["social_media"] = website_info["social_media"]

            prospect = await resolve_prospect(
                session,
                fields,
                source_type=SourceType.google_places,
                job_id=job_id,
                existing_pool=existing_pool,
                stats=stats,
                # Place payloads are intentionally not persisted. Stable Place
                # IDs may be kept; selected fields live on the prospect row.
                raw_source_data={"provider_record_id": place_id},
                source_url=fields.get("google_maps_url"),
                fetched_by=triggered_by,
            )

            if website_info:
                session.add(
                    ProspectSource(
                        prospect_id=prospect.id,
                        job_id=job_id,
                        source_type=SourceType.website_scrape,
                        source_url=fields.get("website"),
                        raw_data=website_info,
                        fetched_by=triggered_by,
                    )
                )
        except Exception:  # noqa: BLE001 -- one bad result must not abort the search
            stats["errors"] += 1


async def _run_paginas_amarillas(
    session: AsyncSession,
    *,
    region: str,
    comuna: str | None,
    keyword: str,
    max_results: int,
    job_id,
    existing_pool: list[dict],
    stats: dict,
    triggered_by: str | None,
) -> None:
    listings = await paginas_amarillas.search(keyword, comuna=comuna, max_results=max_results)
    stats["found_paginas_amarillas"] = len(listings)

    for listing in listings:
        try:
            detail = (
                await paginas_amarillas.get_detail(listing["url"]) if listing.get("url") else None
            )
            fields = _fields_from_paginas_amarillas(listing, detail)
            if not fields.get("name"):
                stats["errors"] += 1
                continue

            # Best-effort region filter for nationwide (no-comuna) searches --
            # only applied when the detail fetch actually told us the region.
            if not comuna and fields.get("region") and fields["region"].lower() != region.lower():
                continue

            await resolve_prospect(
                session,
                fields,
                source_type=SourceType.paginas_amarillas,
                job_id=job_id,
                existing_pool=existing_pool,
                stats=stats,
                raw_source_data={"listing": listing, "detail": detail},
                source_url=listing.get("url"),
                fetched_by=triggered_by,
            )
        except Exception:  # noqa: BLE001 -- one bad result must not abort the search
            stats["errors"] += 1


async def search_prospects(
    session: AsyncSession,
    *,
    region: str,
    comuna: str | None = None,
    keyword: str,
    max_results: int = 10,
    triggered_by: str | None = None,
) -> ResearchJob:
    query = f"{keyword} en {comuna or region}, Chile"

    job = ResearchJob(
        job_type=JobType.manual_search,
        parameters={"region": region, "comuna": comuna, "keyword": keyword, "query": query},
        status=JobStatus.running,
        started_at=datetime.now(timezone.utc),
        triggered_by=triggered_by,
    )
    session.add(job)
    await session.flush()

    stats = {
        "found_google": 0,
        "found_paginas_amarillas": 0,
        "created": 0,
        "merged": 0,
        "needs_review": 0,
        "errors": 0,
        "skipped_budget": 0,
    }
    existing_pool = await load_existing_pool(session)

    source_errors: list[str] = []
    sources_run = 0

    try:
        await _run_google_places(
            session,
            query=query,
            region=region,
            keyword=keyword,
            max_results=max_results,
            job_id=job.id,
            existing_pool=existing_pool,
            stats=stats,
            triggered_by=triggered_by,
        )
        sources_run += 1
    except GooglePlacesError as exc:
        source_errors.append(f"Google Places: {exc}")

    settings = get_settings()
    if settings.paginas_amarillas_enabled and settings.paginas_amarillas_license_confirmed:
        try:
            await _run_paginas_amarillas(
                session,
                region=region,
                comuna=comuna,
                keyword=keyword,
                max_results=max_results,
                job_id=job.id,
                existing_pool=existing_pool,
                stats=stats,
                triggered_by=triggered_by,
            )
            sources_run += 1
        except Exception as exc:  # noqa: BLE001 - licensed source is best-effort
            source_errors.append(f"Paginas Amarillas: {exc}")

    if sources_run == 0:
        job.status = JobStatus.failed
    elif source_errors or stats["errors"]:
        job.status = JobStatus.partial
    else:
        job.status = JobStatus.completed
    job.error_log = "\n".join(source_errors) or None
    job.finished_at = datetime.now(timezone.utc)
    job.stats = stats

    await session.commit()
    return job
