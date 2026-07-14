"""Thin client for the Google Places API (New). Text Search finds
candidates cheaply (Pro-tier fields); Place Details is called only for
candidates that survive the relevance pre-filter, fetching the fuller
Enterprise-tier field set (phone, website, rating). This two-stage fetch is
the main cost-control lever -- see plan section "Scheduler y control de
costos".
"""

import httpx

from app.config import get_settings

PLACES_BASE_URL = "https://places.googleapis.com/v1"

# Field tiers per Google's Places API (New) SKUs.
_PRO_FIELDS = [
    "id",
    "displayName",
    "formattedAddress",
    "addressComponents",
    "location",
    "types",
    "primaryType",
    "businessStatus",
]
_ENTERPRISE_ONLY_FIELDS = [
    "nationalPhoneNumber",
    "internationalPhoneNumber",
    "websiteUri",
    "rating",
    "userRatingCount",
    "googleMapsUri",
]

# Rough per-call cost estimates (USD) -- see plan section 7 for the source
# figures (~$32/1000 Pro tier, ~$35/1000 Enterprise tier as of plan-writing).
COST_ESTIMATE_USD = {"pro": 0.032, "enterprise": 0.035}


class GooglePlacesError(Exception):
    pass


class GooglePlacesClient:
    def __init__(self, api_key: str | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.google_maps_api_key
        if not self.api_key:
            raise GooglePlacesError(
                "GOOGLE_MAPS_API_KEY no esta configurado. Agregalo a tu .env para "
                "activar la busqueda de prospectos en Google Maps."
            )

    async def text_search(self, query: str, *, max_results: int = 20) -> list[dict]:
        """Pro-tier search: cheap, enough to discover + pre-filter candidates."""
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.post(
                f"{PLACES_BASE_URL}/places:searchText",
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": ",".join(f"places.{f}" for f in _PRO_FIELDS),
                    "Content-Type": "application/json",
                },
                json={
                    "textQuery": query,
                    "languageCode": "es",
                    "regionCode": "CL",
                    "maxResultCount": min(max_results, 20),
                },
            )
        if resp.status_code != 200:
            raise GooglePlacesError(
                f"Google Places text search failed with status {resp.status_code}"
            )
        return resp.json().get("places", [])

    async def get_place_details(self, place_id: str) -> dict | None:
        """Enterprise-tier fetch: only call this for candidates that passed
        the relevance pre-filter."""
        fields = _PRO_FIELDS + _ENTERPRISE_ONLY_FIELDS
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.get(
                f"{PLACES_BASE_URL}/places/{place_id}",
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": ",".join(fields),
                },
            )
        if resp.status_code != 200:
            return None
        return resp.json()


def extract_region_comuna(address_components: list[dict] | None) -> tuple[str | None, str | None]:
    """Best-effort region/comuna extraction from Google's addressComponents.
    Chilean results sometimes classify comuna as administrative_area_level_3
    and sometimes as locality -- both are checked. Not yet validated against
    live API responses (see docs/crm_api_notes.md-style open item); refine
    once tested against real Google Places output for Chilean addresses.
    """
    if not address_components:
        return None, None

    region = None
    comuna = None
    for comp in address_components:
        types = comp.get("types", [])
        text = comp.get("longText")
        if "administrative_area_level_1" in types:
            region = text
        elif "administrative_area_level_3" in types and not comuna:
            comuna = text
        elif "locality" in types and not comuna:
            comuna = text
    return region, comuna
