from __future__ import annotations

from typing import Protocol

import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlsplit

from app.config import get_settings
from app.enrichment.google_places import (
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
from app.prospecting.store import WorkerTask
from app.prospecting.validation import normalize_geo
from app.prospecting.validation import is_hvac_relevant


class SourceNotConfigured(RuntimeError):
    pass


class SourceExecutor(Protocol):
    async def search(
        self, task: WorkerTask, snapshot: ProspectingRunSnapshot
    ) -> list[ProspectCandidate]: ...


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

    def __init__(self) -> None:
        self.settings = get_settings()

    async def search(
        self, task: WorkerTask, snapshot: ProspectingRunSnapshot
    ) -> list[ProspectCandidate]:
        if task.source == SourceName.google_places:
            candidates = await self._google(task)
        elif task.source == SourceName.brave_search:
            candidates = await self._brave(task)
        else:
            raise SourceNotConfigured(f"{task.source.value} is not a discovery source")

        if SourceName.official_website in snapshot.campaign.sources:
            candidates = [
                await self._enrich_official_website(candidate, task) for candidate in candidates
            ]
        return candidates

    async def _google(self, task: WorkerTask) -> list[ProspectCandidate]:
        try:
            client = GooglePlacesClient()
        except GooglePlacesError as exc:
            raise SourceNotConfigured(str(exc)) from exc
        query = f"{task.keyword} en {task.comuna_name}, {task.region_name}, Chile"
        places = await client.text_search(query, max_results=task.max_results)
        survivors: list[dict] = []
        for place in places:
            name = (place.get("displayName") or {}).get("text") or ""
            discovery_candidate = ProspectCandidate(
                name=name or "Resultado sin nombre",
                location=_location_for_google(place, task),
                description=" ".join(place.get("types") or []),
            )
            location = discovery_candidate.location
            if (
                is_hvac_relevant(discovery_candidate)
                and location.region_code == task.region_code
                and location.comuna_code == task.comuna_code
            ):
                survivors.append(place)

        candidates: list[ProspectCandidate] = []
        for place in survivors[: task.max_results]:
            place_id = place.get("id")
            details = await client.get_place_details(place_id) if place_id else None
            source = details or place
            name = (source.get("displayName") or {}).get("text")
            if not name:
                continue
            location = _location_for_google(source, task)
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
            candidates.append(
                ProspectCandidate(
                    name=name,
                    provider_ids={"google_places": place_id} if place_id else {},
                    phone=phone,
                    website=website_uri,
                    location=location,
                    description=description,
                    evidence=[item for item in evidence if item is not None],
                )
            )
        return candidates

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
            enrichment.get(field_name)
            for field_name in ("address", "comuna_name", "region_name")
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
