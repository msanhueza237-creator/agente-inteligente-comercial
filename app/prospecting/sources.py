from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlsplit

from app.config import get_settings
from app.db.models import PlacesFieldTier, PlacesQueryType
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
from app.prospecting.contracts import (
    ProspectCandidate,
    ProspectLocation,
    ProspectingRunSnapshot,
    SourceEvidence,
    SourceName,
)
from app.prospecting.budget import GooglePlacesBudget, MemoryGooglePlacesBudget
from app.prospecting.store import WorkerTask
from app.prospecting.validation import normalize_geo
from app.prospecting.validation import is_hvac_relevant


@dataclass(frozen=True)
class SourceSearchResult(Sequence[ProspectCandidate]):
    """Candidates plus operational metrics from a discovery connector."""

    candidates: tuple[ProspectCandidate, ...]
    metrics: dict[str, int | float | str | bool | None] = field(default_factory=dict)

    def __iter__(self) -> Iterator[ProspectCandidate]:
        return iter(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)

    def __getitem__(self, index):
        return self.candidates[index]


class SourceNotConfigured(RuntimeError):
    pass


class SourceExecutor(Protocol):
    async def search(
        self, task: WorkerTask, snapshot: ProspectingRunSnapshot
    ) -> Sequence[ProspectCandidate]: ...


_TARGET_QUERY_PREFIXES = {
    "distribuidor": ("distribuidor", "mayorista", "importador"),
    "tienda comercial": ("tienda", "venta"),
    "tecnico": ("servicio tecnico", "mantencion y reparacion"),
    "instalador grande": ("empresa instaladora", "proyectos comerciales"),
    "competencia": ("empresa",),
    "otro": ("empresa",),
}


def build_google_query_plan(
    task: WorkerTask,
    snapshot: ProspectingRunSnapshot,
    *,
    max_queries: int,
) -> tuple[str, ...]:
    """Expand one CRM task into complementary, deduplicated search intents."""

    location = f"{task.comuna_name}, {task.region_name}, Chile"
    queries = [f"{task.keyword} en {location}"]
    for target_type in snapshot.campaign.target_types:
        for prefix in _TARGET_QUERY_PREFIXES.get(target_type, ()):
            queries.append(f"{prefix} de {task.keyword} en {location}")
    queries.extend(
        (
            f"empresa de {task.keyword} en {location}",
            f"instalacion de {task.keyword} en {location}",
            f"mantencion de {task.keyword} en {location}",
            f"servicio tecnico de {task.keyword} en {location}",
            f"venta de {task.keyword} en {location}",
            f"proveedor de {task.keyword} en {location}",
        )
    )
    unique: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = " ".join(query.casefold().split())
        if key in seen:
            continue
        seen.add(key)
        unique.append(query)
        if len(unique) >= max_queries:
            break
    return tuple(unique)


def _evidence(
    provider: SourceName,
    field: str,
    value: str | None,
    *,
    source_url: str | None,
    provider_record_id: str | None,
) -> SourceEvidence | None:
    if value is None or not str(value).strip():
        return None
    return SourceEvidence(
        provider=provider,
        source_url=source_url,
        provider_record_id=provider_record_id,
        field=field,
        value=str(value),
    )


def _location_for_google(place: dict, task: WorkerTask) -> ProspectLocation:
    region, comuna = extract_region_comuna(place.get("addressComponents"))
    region_matches = normalize_geo(region) == normalize_geo(task.region_name)
    comuna_matches = normalize_geo(comuna) == normalize_geo(task.comuna_name)
    return ProspectLocation(
        region_code=task.region_code if region_matches else None,
        region_name=region,
        comuna_code=task.comuna_code if comuna_matches else None,
        comuna_name=comuna,
        address=place.get("formattedAddress"),
    )


class AuthorizedSourceExecutor:
    """Only licensed APIs plus the company's own public website are used."""

    def __init__(self, budget: GooglePlacesBudget | None = None) -> None:
        self.settings = get_settings()
        self.budget = budget or MemoryGooglePlacesBudget(self.settings)

    async def search(
        self, task: WorkerTask, snapshot: ProspectingRunSnapshot
    ) -> SourceSearchResult:
        if task.source == SourceName.google_places:
            result = await self._google(task, snapshot)
            candidates = list(result)
            metrics = dict(result.metrics)
        elif task.source == SourceName.brave_search:
            candidates = await self._brave(task)
            metrics = {
                "queries_executed": 1,
                "unique_results": len(candidates),
            }
        else:
            raise SourceNotConfigured(f"{task.source.value} is not a discovery source")

        if SourceName.official_website in snapshot.campaign.sources:
            candidates = [
                await self._enrich_official_website(candidate, task) for candidate in candidates
            ]
        return SourceSearchResult(tuple(candidates), metrics)

    async def _google(
        self, task: WorkerTask, snapshot: ProspectingRunSnapshot
    ) -> SourceSearchResult:
        try:
            client = GooglePlacesClient()
        except GooglePlacesError as exc:
            raise SourceNotConfigured(str(exc)) from exc
        queries = build_google_query_plan(
            task,
            snapshot,
            max_queries=self.settings.google_places_queries_per_task,
        )
        unique_places: dict[str, dict] = {}
        raw_results = 0
        queries_executed = 0
        budget_limited = False
        budget_reason: str | None = None
        budget_alert = False
        run_spend = 0.0
        daily_spend = 0.0
        monthly_spend = 0.0
        for query in queries:
            reservation = await self.budget.reserve(
                run_id=snapshot.crm_run_id,
                task_id=task.id,
                query_type=PlacesQueryType.text_search,
                tier=PlacesFieldTier.pro,
                region=task.region_name,
                keyword=task.keyword,
            )
            if not reservation.allowed:
                budget_limited = True
                budget_reason = reservation.reason
                break
            places = await client.text_search(query, max_results=task.max_results)
            queries_executed += 1
            await self.budget.complete(reservation, len(places))
            budget_alert = budget_alert or reservation.alert
            run_spend = reservation.run_spend_usd
            daily_spend = reservation.daily_spend_usd
            monthly_spend = reservation.monthly_spend_usd
            raw_results += len(places)
            for place in places:
                place_id = place.get("id")
                fallback_key = "|".join(
                    (
                        ((place.get("displayName") or {}).get("text") or "").casefold(),
                        (place.get("formattedAddress") or "").casefold(),
                    )
                )
                key = place_id or fallback_key
                if key.strip("|"):
                    unique_places.setdefault(key, place)

        survivors: list[tuple[int, dict]] = []
        outside_territory = 0
        for place in unique_places.values():
            name = (place.get("displayName") or {}).get("text") or ""
            discovery_candidate = ProspectCandidate(
                name=name or "Resultado sin nombre",
                location=_location_for_google(place, task),
                description=" ".join(place.get("types") or []),
            )
            location = discovery_candidate.location
            if location.region_code != task.region_code or location.comuna_code != task.comuna_code:
                outside_territory += 1
                continue
            # Prefer explicit HVAC signals, but do not discard generic Google
            # categories before Place Details can provide contact information.
            priority = 1 if is_hvac_relevant(discovery_candidate) else 0
            survivors.append((priority, place))
        survivors.sort(key=lambda item: item[0], reverse=True)

        candidates: list[ProspectCandidate] = []
        detail_limit = min(
            len(survivors),
            task.max_results * self.settings.google_places_detail_multiplier,
        )
        details_requested = 0
        for _priority, place in survivors[:detail_limit]:
            place_id = place.get("id")
            details = None
            if place_id:
                reservation = await self.budget.reserve(
                    run_id=snapshot.crm_run_id,
                    task_id=task.id,
                    query_type=PlacesQueryType.place_details,
                    tier=PlacesFieldTier.enterprise,
                    region=task.region_name,
                    keyword=task.keyword,
                )
                if not reservation.allowed:
                    budget_limited = True
                    budget_reason = reservation.reason
                    break
                details = await client.get_place_details(place_id)
                await self.budget.complete(reservation, int(details is not None))
                budget_alert = budget_alert or reservation.alert
                run_spend = reservation.run_spend_usd
                daily_spend = reservation.daily_spend_usd
                monthly_spend = reservation.monthly_spend_usd
            details_requested += int(bool(place_id))
            source = details or place
            name = (source.get("displayName") or {}).get("text")
            if not name:
                continue
            location = _location_for_google(source, task)
            if location.region_code != task.region_code or location.comuna_code != task.comuna_code:
                outside_territory += 1
                continue
            source_url = source.get("googleMapsUri")
            phone = normalize_phone(
                source.get("nationalPhoneNumber") or source.get("internationalPhoneNumber")
            )
            website_uri = source.get("websiteUri")
            description = " ".join(source.get("types") or [])
            evidence = [
                _evidence(
                    SourceName.google_places,
                    "name",
                    name,
                    source_url=source_url,
                    provider_record_id=place_id,
                ),
                _evidence(
                    SourceName.google_places,
                    "description",
                    description,
                    source_url=source_url,
                    provider_record_id=place_id,
                ),
                _evidence(
                    SourceName.google_places,
                    "location.region_code",
                    location.region_code,
                    source_url=source_url,
                    provider_record_id=place_id,
                ),
                _evidence(
                    SourceName.google_places,
                    "location.region_name",
                    location.region_name,
                    source_url=source_url,
                    provider_record_id=place_id,
                ),
                _evidence(
                    SourceName.google_places,
                    "location.comuna_code",
                    location.comuna_code,
                    source_url=source_url,
                    provider_record_id=place_id,
                ),
                _evidence(
                    SourceName.google_places,
                    "location.comuna_name",
                    location.comuna_name,
                    source_url=source_url,
                    provider_record_id=place_id,
                ),
                _evidence(
                    SourceName.google_places,
                    "location.address",
                    location.address,
                    source_url=source_url,
                    provider_record_id=place_id,
                ),
                _evidence(
                    SourceName.google_places,
                    "phone",
                    phone,
                    source_url=source_url,
                    provider_record_id=place_id,
                ),
                _evidence(
                    SourceName.google_places,
                    "website",
                    website_uri,
                    source_url=source_url,
                    provider_record_id=place_id,
                ),
            ]
            candidate = ProspectCandidate(
                name=name,
                provider_ids={"google_places": place_id} if place_id else {},
                phone=phone,
                website=website_uri,
                location=location,
                description=description,
                evidence=[item for item in evidence if item is not None],
            )
            if not is_hvac_relevant(candidate):
                candidate = candidate.model_copy(
                    update={
                        "review_flags": (
                            *candidate.review_flags,
                            "hvac_query_match",
                            "hvac_relevance_needs_review",
                        )
                    }
                )
            candidates.append(candidate)
        return SourceSearchResult(
            tuple(candidates),
            {
                "queries_planned": len(queries),
                "queries_executed": queries_executed,
                "raw_results": raw_results,
                "unique_results": len(unique_places),
                "outside_territory_discovery": outside_territory,
                "details_requested": details_requested,
                "candidates_prepared": len(candidates),
                "estimated_google_cost_usd": round(
                    queries_executed * COST_ESTIMATE_USD["pro"]
                    + details_requested * COST_ESTIMATE_USD["enterprise"],
                    4,
                ),
                "budget_limited": budget_limited,
                "budget_reason": budget_reason,
                "budget_alert": budget_alert,
                "run_spend_usd": round(run_spend, 4),
                "daily_spend_usd": round(daily_spend, 4),
                "monthly_spend_usd": round(monthly_spend, 4),
                "run_budget_usd": self.settings.google_places_run_budget_usd,
            },
        )

    async def _brave(self, task: WorkerTask) -> list[ProspectCandidate]:
        if not self.settings.brave_search_api_key:
            raise SourceNotConfigured("BRAVE_SEARCH_API_KEY is not configured")
        query = f'"{task.comuna_name}" {task.keyword} Chile'
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.settings.brave_search_api_key,
                },
                params={
                    "q": query,
                    "count": min(task.max_results, 20),
                    "country": "cl",
                    "search_lang": "es",
                    "safesearch": "moderate",
                },
            )
        if response.status_code != 200:
            raise RuntimeError(f"Brave Search failed with status {response.status_code}")

        candidates: list[ProspectCandidate] = []
        for result in response.json().get("web", {}).get("results", []):
            url = result.get("url")
            title = BeautifulSoup(result.get("title") or "", "lxml").get_text(" ").strip()
            description = (
                BeautifulSoup(result.get("description") or "", "lxml").get_text(" ").strip()
            )
            if not title or not url:
                continue
            # Search snippets describe service coverage, not necessarily a
            # business domicile. Geographic CUT data is only assigned later
            # when an official structured address corroborates the task area.
            location = ProspectLocation()
            parsed_url = urlsplit(url)
            provider_id = parsed_url._replace(query="", fragment="").geturl().rstrip("/")
            evidence = [
                _evidence(
                    SourceName.brave_search,
                    "name",
                    title,
                    source_url=url,
                    provider_record_id=provider_id,
                ),
                _evidence(
                    SourceName.brave_search,
                    "website",
                    url,
                    source_url=url,
                    provider_record_id=provider_id,
                ),
                _evidence(
                    SourceName.brave_search,
                    "description",
                    description,
                    source_url=url,
                    provider_record_id=provider_id,
                ),
            ]
            candidate = ProspectCandidate(
                name=title,
                provider_ids={"brave_search": provider_id},
                website=url,
                location=location,
                description=description,
                evidence=[item for item in evidence if item is not None],
            )
            if not self._is_official_website_candidate(candidate):
                candidate = candidate.model_copy(
                    update={
                        "website": None,
                        "evidence": [
                            item for item in candidate.evidence if item.field != "website"
                        ],
                    }
                )
            candidates.append(candidate)
        return candidates

    async def _enrich_official_website(
        self, candidate: ProspectCandidate, task: WorkerTask
    ) -> ProspectCandidate:
        if not self._is_official_website_candidate(candidate):
            return candidate
        enrichment = await enrich_from_website(candidate.website)
        if not enrichment:
            return candidate
        source_url = enrichment.get("source_url") or candidate.website
        provider_id = normalize_website(candidate.website)
        evidence = list(candidate.evidence)

        def add(field: str, value: str | None, *, url: str | None = None) -> None:
            item = _evidence(
                SourceName.official_website,
                field,
                value,
                source_url=url or source_url,
                provider_record_id=provider_id,
            )
            if item is not None:
                evidence.append(item)

        add("website", candidate.website)
        website_name = enrichment.get("name")
        candidate_name = normalize_name(candidate.name)[0]
        if website_name and normalize_name(website_name)[0] == candidate_name:
            add("name", website_name)

        website_locations = list(enrichment.get("locations") or [])
        if not website_locations and any(
            enrichment.get(field_name) for field_name in ("address", "comuna_name", "region_name")
        ):
            website_locations.append(
                {
                    "address": enrichment.get("address"),
                    "comuna_name": enrichment.get("comuna_name"),
                    "region_name": enrichment.get("region_name"),
                    "source_url": source_url,
                }
            )

        prepared_locations: list[ProspectLocation] = []
        for location in candidate.locations:
            prepared = location
            for website_location in website_locations:
                website_comuna = website_location.get("comuna_name")
                website_region = website_location.get("region_name")
                website_address = website_location.get("address")
                if normalize_geo(website_comuna) != normalize_geo(task.comuna_name):
                    continue
                if website_region and normalize_geo(website_region) != normalize_geo(
                    task.region_name
                ):
                    continue
                if (
                    location.address
                    and website_address
                    and normalize_address(location.address) != normalize_address(website_address)
                ):
                    continue
                prepared = location.model_copy(
                    update={
                        "region_code": task.region_code,
                        "region_name": website_region or location.region_name or task.region_name,
                        "comuna_code": task.comuna_code,
                        "comuna_name": website_comuna,
                        "address": website_address or location.address,
                    }
                )
                location_source = website_location.get("source_url") or source_url
                add("location.region_code", prepared.region_code, url=location_source)
                add("location.region_name", prepared.region_name, url=location_source)
                add("location.comuna_code", prepared.comuna_code, url=location_source)
                add("location.comuna_name", prepared.comuna_name, url=location_source)
                add("location.address", prepared.address, url=location_source)
                break
            prepared_locations.append(prepared)

        email = enrichment.get("email")
        phone = normalize_phone(enrichment.get("phone"))
        add("email", email)
        add("phone", phone)
        canonical = prepared_locations[0]
        return candidate.model_copy(
            update={
                "email": email or candidate.email,
                "phone": phone or candidate.phone,
                "location": canonical,
                "locations": prepared_locations,
                "provider_ids": {
                    **candidate.provider_ids,
                    **({"official_website": provider_id} if provider_id else {}),
                },
                "evidence": evidence,
            }
        )

    @staticmethod
    def _is_official_website_candidate(candidate: ProspectCandidate) -> bool:
        if not candidate.website or not normalize_website(candidate.website):
            return False
        parsed = urlsplit(
            candidate.website if "://" in candidate.website else f"https://{candidate.website}"
        )
        if parsed.scheme not in {"http", "https"}:
            return False
        domain = normalize_website(candidate.website) or ""
        blocked_domains = {
            "amarillas.cl",
            "google.com",
            "google.cl",
            "facebook.com",
            "instagram.com",
            "linkedin.com",
            "twitter.com",
            "x.com",
            "mercadolibre.cl",
            "yapo.cl",
            "starofservice.cl",
            "tripadvisor.cl",
        }
        if domain in blocked_domains:
            return False
        # A website declared by a Google Business profile is considered the
        # business site. Brave-only hits need a name/domain affinity signal.
        if candidate.provider_ids.get("google_places"):
            return True
        title_tokens = {
            token
            for token in normalize_geo(candidate.name).split()
            if len(token) >= 4
            and token not in {"CLIMA", "CLIMATIZACION", "HVAC", "SERVICIO", "SERVICIOS", "CHILE"}
        }
        domain_label = domain.split(".")[0].upper()
        return any(token in domain_label for token in title_tokens)


class StaticSourceExecutor:
    """Development/test executor; never makes a network request."""

    def __init__(self, candidates: list[ProspectCandidate] | None = None):
        self.candidates = candidates or []
        self.calls: list[WorkerTask] = []

    async def search(
        self, task: WorkerTask, snapshot: ProspectingRunSnapshot
    ) -> list[ProspectCandidate]:
        del snapshot
        self.calls.append(task)
        return [candidate.model_copy(deep=True) for candidate in self.candidates]
