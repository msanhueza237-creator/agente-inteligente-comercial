from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol
import json
import uuid

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
from app.normalization.phone import normalize_phone, normalize_whatsapp_number
from app.normalization.website import normalize_website
from app.prospecting.contracts import (
    DerivedProvenance,
    ProspectCandidate,
    ProspectLocation,
    ProspectingRunSnapshot,
    SourceEvidence,
    SourceName,
)
from app.prospecting.budget import (
    GooglePlacesBudget,
    MemoryGooglePlacesBudget,
    PersistentBraveSearchBudget,
    brave_provider_usage_from_headers,
)
from app.prospecting.dedup import merge_exact_candidate
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
    "distribuidor": ("distribuidor", "mayorista", "importador", "proveedor"),
    "tienda comercial": ("tienda", "repuestos", "venta", "insumos"),
    "tecnico": ("servicio tecnico", "mantencion y reparacion"),
    "instalador grande": ("empresa instaladora", "proyectos comerciales"),
    "competencia": ("empresa", "proyectos HVAC", "refrigeracion industrial"),
    "otro": ("empresa",),
}

_SPECIALTY_LABELS = {
    "aire acondicionado": "aire acondicionado",
    "climatizacion": "climatización",
    "refrigeracion": "refrigeración",
    "ventilacion": "ventilación",
    "calefaccion": "calefacción",
    "mantencion": "mantención",
    "mantenimiento": "mantenimiento",
    "instalacion": "instalación",
    "servicio tecnico": "servicio técnico",
    "proyecto hvac": "proyectos HVAC",
    "camara frigorifica": "cámaras frigoríficas",
    "extraccion de aire": "extracción de aire",
    "automatizacion": "automatización",
    "eficiencia energetica": "eficiencia energética",
}


def _spanish_list(values: Sequence[str]) -> str:
    prepared = [value for value in values if value]
    if len(prepared) < 2:
        return prepared[0] if prepared else ""
    return f"{', '.join(prepared[:-1])} y {prepared[-1]}"


def build_company_summary(candidate: ProspectCandidate) -> str:
    """Create a cautious Spanish summary using only persisted public signals."""

    comuna = candidate.location.comuna_name or candidate.location.region_name
    location_text = f" con actividad en {comuna}" if comuna else " en Chile"
    specialties = tuple(
        dict.fromkeys(_SPECIALTY_LABELS.get(value.casefold(), value) for value in candidate.specialties)
    )[:5]
    brands = tuple(dict.fromkeys(candidate.brands))[:5]
    category = (candidate.category or "").strip().casefold()
    official_description = next(
        (
            " ".join(item.value.split())[:350].rstrip(" ,;:-")
            for item in candidate.evidence
            if item.provider == SourceName.official_website
            and item.field == "description"
            and item.value.strip()
        ),
        None,
    )

    if specialties:
        sentences = [
            f"Empresa del sector climatización y HVAC{location_text}.",
            f"En sus fuentes públicas se identifican servicios de {_spanish_list(specialties)}.",
        ]
    elif category and category != "otro":
        sentences = [
            f"Candidato comercial del sector climatización y HVAC{location_text}, clasificado como {category}.",
            "La actividad específica requiere confirmación cuando no está detallada en su sitio público.",
        ]
    else:
        sentences = [
            f"Empresa encontrada durante una búsqueda de climatización y HVAC{location_text}.",
            "No fue posible confirmar públicamente su actividad específica.",
        ]

    if brands:
        sentences.append(f"En su información pública se mencionan marcas como {_spanish_list(brands)}.")
    if official_description:
        sentences.append(f"Su sitio oficial indica: {official_description.rstrip('.')}.")
    channels = []
    if candidate.website:
        channels.append("sitio web")
    if candidate.email:
        channels.append("correo")
    if candidate.phone:
        channels.append("teléfono")
    if candidate.whatsapp_number:
        channels.append("WhatsApp")
    if candidate.social_media:
        channels.append("redes sociales")
    if channels:
        sentences.append(f"Dispone de contacto mediante {_spanish_list(channels)}.")
    return " ".join(sentences)[:1200]


def build_google_query_plan(
    task: WorkerTask,
    snapshot: ProspectingRunSnapshot,
    *,
    max_queries: int,
) -> tuple[str, ...]:
    """Expand one CRM task into complementary, deduplicated search intents."""

    location = f"{task.comuna_name}, {task.region_name}, Chile"
    queries = [f"{task.keyword} en {location}"]
    # Interleave the commercial roles.  This prevents the first selected
    # target type from consuming the whole request budget and gives one
    # relevant query to distributors, stores and competitors alike.
    role_prefixes = [
        _TARGET_QUERY_PREFIXES.get(target_type, ())
        for target_type in snapshot.campaign.target_types
    ]
    for prefix_index in range(max((len(prefixes) for prefixes in role_prefixes), default=0)):
        for prefixes in role_prefixes:
            if prefix_index < len(prefixes):
                queries.append(f"{prefixes[prefix_index]} de {task.keyword} en {location}")
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


def is_market_radar(snapshot: ProspectingRunSnapshot) -> bool:
    targets = set(snapshot.campaign.target_types)
    return bool(targets & {"distribuidor", "tienda comercial", "competencia"}) and not bool(
        targets & {"tecnico", "instalador grande"}
    )


def build_brave_market_query_plan(task: WorkerTask, *, max_queries: int) -> tuple[str, ...]:
    region = task.region_name or task.comuna_name
    keyword = task.keyword
    intents = (
        f'"{keyword}" distribuidor Chile',
        f'"{keyword}" mayorista Chile',
        f'"{keyword}" importador Chile',
        f'"{keyword}" repuestos Chile',
        f'"{keyword}" tienda {region} Chile',
        f'"{keyword}" proveedor {region} Chile',
        f'"{keyword}" marcas catalogo Chile',
        f'"{keyword}" empresa refrigeracion climatizacion Chile',
        f'"{keyword}" sucursales Chile',
        f'"{keyword}" venta insumos HVAC Chile',
    )
    return intents[:max_queries]


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

    def __init__(self, budget: GooglePlacesBudget | None = None, brave_budget: PersistentBraveSearchBudget | None = None) -> None:
        self.settings = get_settings()
        self.budget = budget or MemoryGooglePlacesBudget(self.settings)
        self.brave_budget = brave_budget

    async def search(
        self, task: WorkerTask, snapshot: ProspectingRunSnapshot
    ) -> SourceSearchResult:
        if task.source == SourceName.google_places:
            result = await self._google(task, snapshot)
            candidates = list(result)
            metrics = dict(result.metrics)
        elif task.source == SourceName.brave_search:
            result = await self._brave(task, snapshot)
            candidates = list(result.candidates)
            metrics = dict(result.metrics)
        else:
            raise SourceNotConfigured(f"{task.source.value} is not a discovery source")

        # Google is the primary discovery pass. Brave is staged by the worker:
        # exact Google matches are merged without crawling the same site again,
        # while only genuinely new Brave hits reach official-site research.
        if (
            task.source == SourceName.google_places
            and SourceName.official_website in snapshot.campaign.sources
        ):
            candidates = [
                await self._enrich_official_website(candidate, task, snapshot) for candidate in candidates
            ]
        return SourceSearchResult(tuple(candidates), metrics)

    async def enrich_discovered(
        self, candidate: ProspectCandidate, task: WorkerTask, snapshot: ProspectingRunSnapshot | None = None
    ) -> ProspectCandidate:
        """Research one novel discovery using its public official website."""
        return await self._enrich_official_website(candidate, task, snapshot)

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
            whatsapp_number = normalize_whatsapp_number(phone)
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
                    "whatsapp_number",
                    whatsapp_number,
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
                whatsapp_number=whatsapp_number,
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

    async def _brave(self, task: WorkerTask, snapshot: ProspectingRunSnapshot) -> SourceSearchResult:
        if not self.settings.brave_search_api_key:
            raise SourceNotConfigured("BRAVE_SEARCH_API_KEY is not configured")
        radar = is_market_radar(snapshot)
        region_territories = [item for item in snapshot.campaign.territories if item.region_code == task.region_code]
        anchor = min((item.comuna_code for item in region_territories), default=task.comuna_code)
        if radar and task.comuna_code != anchor:
            return SourceSearchResult((), {"queries_executed": 0, "radar_region_anchor_skipped": True})
        queries = build_brave_market_query_plan(
            task, max_queries=self.settings.brave_market_queries_per_region
        ) if radar else (f'"{task.comuna_name}" {task.keyword} Chile',)
        query_plan: list[tuple[str, str]] = [(query, "discovery") for query in queries]
        if snapshot.brave_policy.social_search_enabled and task.comuna_code == anchor:
            social_limit = snapshot.brave_policy.max_social_queries_per_campaign
            social_queries = (
                f'site:instagram.com {task.keyword} "{task.region_name or task.comuna_name}" Chile',
                f'site:facebook.com {task.keyword} "{task.region_name or task.comuna_name}" Chile',
            )
            query_plan.extend((query, "social") for query in social_queries[:social_limit])
        results: list[tuple[int, dict]] = []
        budget_limited = False
        monthly_spend = 0.0
        queries_executed = 0
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            social_queries_executed = 0
            for query, query_kind in query_plan:
                reservation = None
                if self.brave_budget:
                    reservation = await self.brave_budget.reserve(
                        run_id=snapshot.crm_run_id, task_id=task.id, query=query,
                        monthly_limit_usd=snapshot.brave_policy.monthly_limit_usd, query_kind=query_kind,
                        max_social_queries=snapshot.brave_policy.max_social_queries_per_campaign,
                    )
                    monthly_spend = reservation.monthly_spend_usd
                    if not reservation.allowed:
                        if reservation.reason == "social_query_limit_exhausted":
                            continue
                        budget_limited = True
                        break
                response = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={"Accept": "application/json", "X-Subscription-Token": self.settings.brave_search_api_key},
                    params={"q": query, "count": min(task.max_results, 20), "country": "cl", "search_lang": "es", "safesearch": "moderate"},
                )
                if response.status_code != 200:
                    raise RuntimeError(f"Brave Search failed with status {response.status_code}")
                if self.brave_budget:
                    provider_usage = brave_provider_usage_from_headers(response.headers)
                    if provider_usage:
                        provider_queries, provider_limit, reset_seconds = provider_usage
                        summary = await self.brave_budget.reconcile_provider_usage(
                            provider_queries=provider_queries,
                            provider_limit_queries=provider_limit,
                            provider_remaining_queries=max(0, provider_limit - provider_queries),
                            reset_seconds=reset_seconds,
                        )
                        monthly_spend = float(summary["monthly_spend_usd"])
                web_results = response.json().get("web", {}).get("results", [])
                queries_executed += 1
                social_queries_executed += int(query_kind == "social")
                results.extend(enumerate(web_results, start=1))
                if reservation:
                    await self.brave_budget.complete(reservation, len(web_results))

        by_domain: dict[str, ProspectCandidate] = {}
        for rank, result in results:
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
                market_signals={"query_hits": 1, "best_rank": rank, "radar_mode": radar},
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
            if radar and not candidate.website:
                continue
            domain = normalize_website(candidate.website or url) or provider_id
            existing = by_domain.get(domain)
            by_domain[domain] = merge_exact_candidate(existing, candidate) if existing else candidate
        candidates = list(by_domain.values())
        candidates.sort(key=lambda item: (-int(item.market_signals.get("query_hits", 0)), int(item.market_signals.get("best_rank", 99))))
        return SourceSearchResult(tuple(candidates), {"queries_executed": queries_executed, "social_queries_executed": social_queries_executed, "raw_results": len(results), "unique_results": len(candidates), "market_radar": radar, "budget_limited": budget_limited, "budget_reason": "monthly_budget_exhausted" if budget_limited else None, "monthly_spend_usd": round(monthly_spend, 4)})

    async def _enrich_official_website(
        self, candidate: ProspectCandidate, task: WorkerTask,
        snapshot: ProspectingRunSnapshot | None = None,
    ) -> ProspectCandidate:
        if not self._is_official_website_candidate(candidate):
            return candidate
        enrichment = await enrich_from_website(candidate.website)
        if not enrichment:
            return candidate
        source_url = enrichment.get("source_url") or candidate.website
        field_sources = enrichment.get("field_sources") or {}
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

        territories = list(snapshot.campaign.territories) if snapshot else []
        if not territories:
            territories = [type("TaskTerritory", (), {
                "region_code": task.region_code, "region_name": task.region_name,
                "comuna_code": task.comuna_code, "comuna_name": task.comuna_name,
            })()]
        prepared_locations: list[ProspectLocation] = []
        for website_location in website_locations:
            website_comuna = website_location.get("comuna_name")
            website_region = website_location.get("region_name")
            target = next((territory for territory in territories
                if normalize_geo(website_comuna) == normalize_geo(territory.comuna_name)
                and (not website_region or normalize_geo(website_region) == normalize_geo(territory.region_name))), None)
            if target is None:
                continue
            prepared = ProspectLocation(
                region_code=target.region_code, region_name=website_region or target.region_name,
                comuna_code=target.comuna_code, comuna_name=website_comuna or target.comuna_name,
                address=website_location.get("address"),
            )
            if not any(normalize_address(item.address) == normalize_address(prepared.address) and item.comuna_code == prepared.comuna_code for item in prepared_locations):
                prepared_locations.append(prepared)
                location_source = website_location.get("source_url") or source_url
                add("location.region_code", prepared.region_code, url=location_source)
                add("location.region_name", prepared.region_name, url=location_source)
                add("location.comuna_code", prepared.comuna_code, url=location_source)
                add("location.comuna_name", prepared.comuna_name, url=location_source)
                add("location.address", prepared.address, url=location_source)
        matched_official_location = bool(prepared_locations)
        if not prepared_locations:
            prepared_locations = list(candidate.locations)
        review_flags = list(candidate.review_flags)
        if website_locations and not matched_official_location and "official_location_conflict" not in review_flags:
            review_flags.append("official_location_conflict")

        email = enrichment.get("email")
        phone = normalize_phone(enrichment.get("phone"))
        whatsapp_number = normalize_whatsapp_number(enrichment.get("whatsapp_number") or phone)
        website_description = enrichment.get("description")
        add("email", email, url=field_sources.get("email"))
        add("phone", phone, url=field_sources.get("phone"))
        add("whatsapp_number", whatsapp_number, url=field_sources.get("whatsapp_number") or field_sources.get("phone"))
        add("description", website_description, url=field_sources.get("description"))
        social_media = enrichment.get("social_media") or {}
        specialties = tuple(enrichment.get("specialties") or ())
        brands = tuple(enrichment.get("brands") or ())
        for platform, url in social_media.items():
            add(
                f"social_media.{platform}",
                url,
                url=field_sources.get(f"social_media.{platform}") or source_url,
            )
        if specialties:
            add("specialties", json.dumps(specialties, ensure_ascii=False), url=source_url)
        if brands:
            add("brands", json.dumps(brands, ensure_ascii=False), url=source_url)
        canonical = prepared_locations[0]
        return candidate.model_copy(
            update={
                "email": email or candidate.email,
                "phone": phone or candidate.phone,
                "whatsapp_number": whatsapp_number or candidate.whatsapp_number,
                "description": website_description or candidate.description,
                "social_media": {**candidate.social_media, **social_media},
                "specialties": tuple(dict.fromkeys((*candidate.specialties, *specialties))),
                "brands": tuple(dict.fromkeys((*candidate.brands, *brands))),
                "location": canonical,
                "locations": prepared_locations,
                "provider_ids": {
                    **candidate.provider_ids,
                    **({"official_website": provider_id} if provider_id else {}),
                },
                "evidence": evidence,
                "review_flags": tuple(review_flags),
            }
        )

    async def enrich_existing(self, candidate: ProspectCandidate, run_id: str) -> tuple[ProspectCandidate, dict]:
        """Investigate one persisted candidate without rerunning discovery."""

        prepared = candidate
        website_available = self._is_official_website_candidate(prepared)
        if not website_available:
            prepared = prepared.model_copy(update={
                "review_flags": tuple(dict.fromkeys((*prepared.review_flags, "official_site_missing")))
            })

        location = prepared.location
        task = WorkerTask(
            id=f"enrichment-{uuid.uuid4()}", run_id=run_id, source=SourceName.official_website,
            keyword=prepared.name, region_code=location.region_code or "",
            region_name=location.region_name or "", comuna_code=location.comuna_code or "",
            comuna_name=location.comuna_name or "", max_results=1, attempt_count=0, max_attempts=3,
        )
        before_evidence = len(prepared.evidence)
        enriched = await self._enrich_official_website(prepared, task)
        summary_text = build_company_summary(enriched)
        summary_inputs = tuple(
            field
            for field, present in (
                ("category", bool(enriched.category)),
                ("location", bool(enriched.location.comuna_name or enriched.location.region_name)),
                ("specialties", bool(enriched.specialties)),
                ("brands", bool(enriched.brands)),
                ("website", bool(enriched.website)),
                ("email", bool(enriched.email)),
                ("phone", bool(enriched.phone)),
                ("whatsapp_number", bool(enriched.whatsapp_number)),
                ("social_media", bool(enriched.social_media)),
            )
            if present
        )
        enriched = enriched.model_copy(
            update={
                "company_summary": summary_text,
                "derived_provenance": {
                    **enriched.derived_provenance,
                    "company_summary": DerivedProvenance(
                        ruleset="company-summary-v1",
                        input_fields=summary_inputs,
                    ),
                },
            }
        )
        official_evidence = [item for item in enriched.evidence if item.provider == SourceName.official_website]
        return enriched, {
            "hvac_relevant": is_hvac_relevant(enriched),
            "website_found": bool(enriched.website),
            "website_discovered_by_brave": False,
            "brave_queries_used": 0,
            "official_site_missing": not website_available,
            "official_fields_added": max(0, len(enriched.evidence) - before_evidence),
            "emails_found": int(bool(enriched.email)),
            "phones_found": int(bool(enriched.phone)),
            "social_profiles_found": len(enriched.social_media),
            "specialties_found": len(enriched.specialties),
            "brands_found": len(enriched.brands),
            "official_pages_with_evidence": len({item.source_url for item in official_evidence if item.source_url}),
            "company_summary_created": True,
        }

    async def _find_official_website(self, candidate: ProspectCandidate) -> str | None:
        if not self.settings.brave_search_api_key:
            return None
        location = candidate.location.comuna_name or candidate.location.region_name or "Chile"
        query = f'"{candidate.name}" {location} sitio oficial'
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "X-Subscription-Token": self.settings.brave_search_api_key},
                params={"q": query, "count": 5, "country": "cl", "search_lang": "es", "safesearch": "moderate"},
            )
        if response.status_code != 200:
            raise RuntimeError(f"Brave Search failed with status {response.status_code}")
        for result in response.json().get("web", {}).get("results", []):
            url = result.get("url")
            if not url:
                continue
            probe = candidate.model_copy(update={"website": url, "provider_ids": {"brave_search": url}})
            if self._is_official_website_candidate(probe):
                return url
        return None

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

    async def enrich_existing(self, candidate: ProspectCandidate, run_id: str) -> tuple[ProspectCandidate, dict]:
        del run_id
        return candidate, {"website_found": bool(candidate.website)}

    async def search(
        self, task: WorkerTask, snapshot: ProspectingRunSnapshot
    ) -> list[ProspectCandidate]:
        del snapshot
        self.calls.append(task)
        return [candidate.model_copy(deep=True) for candidate in self.candidates]
