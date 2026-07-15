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

# Public list prices per successful request before monthly free usage caps.
# Text Search Pro: $32/1000; Place Details Enterprise: $20/1000 (2026-07-15).
COST_ESTIMATE_USD = {"pro": 0.032, "enterprise": 0.020}


class GooglePlacesError(Exception):
    pass


class GooglePlacesClient:
    def __init__(
        self,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        settings = get_settings()
        self.api_key = api_key or settings.google_maps_api_key
        if not self.api_key:
            raise GooglePlacesError(
                "GOOGLE_MAPS_API_KEY no esta configurado. Agregalo a tu .env para "
                "activar la busqueda de prospectos en Google Maps."
            )
        self.transport = transport

    async def text_search(self, query: str, *, max_results: int = 20) -> list[dict]:
        """Pro-tier search: cheap, enough to discover + pre-filter candidates."""
        async with httpx.AsyncClient(
            timeout=15, trust_env=False, transport=self.transport
        ) as client:
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
                    "pageSize": min(max_results, 20),
                },
            )
        if resp.status_code != 200:
            raise GooglePlacesError(
                f"Google Places text search failed with status {resp.status_code}"
            )
        return resp.json().get("places", [])

    async def check_connection(self) -> dict[str, object]:
        """Run one minimal Places request without returning provider payloads.

        Only a place identifier is requested. The API key and Google's response
        body are deliberately excluded from the result and from exceptions.
        """
        try:
            async with httpx.AsyncClient(
                timeout=15, trust_env=False, transport=self.transport
            ) as client:
                response = await client.post(
                    f"{PLACES_BASE_URL}/places:searchText",
                    headers={
                        "X-Goog-Api-Key": self.api_key,
                        "X-Goog-FieldMask": "places.id",
                        "Content-Type": "application/json",
                    },
                    json={
                        "textQuery": "climatizacion en Santiago, Chile",
                        "languageCode": "es",
                        "regionCode": "CL",
                        "pageSize": 1,
                    },
                )
        except httpx.TimeoutException:
            return {
                "status": "error",
                "error_code": "timeout",
                "message": "Google Places no respondio dentro del tiempo esperado.",
            }
        except httpx.TransportError:
            return {
                "status": "error",
                "error_code": "network_error",
                "message": "No fue posible conectar con Google Places desde el agente.",
            }

        if response.status_code == 200:
            return {
                "status": "connected",
                "error_code": None,
                "message": "Google Places respondio correctamente.",
            }
        if response.status_code == 429:
            return {
                "status": "quota_exhausted",
                "error_code": "quota_exhausted",
                "message": "Google Places rechazo la prueba por cuota o limite de consumo.",
            }
        if response.status_code == 403:
            return {
                "status": "error",
                "error_code": "forbidden",
                "message": (
                    "Google Places rechazo la credencial. Revisa API habilitada, "
                    "facturacion y restricciones de la clave."
                ),
            }
        if response.status_code >= 500:
            return {
                "status": "error",
                "error_code": "provider_unavailable",
                "message": "Google Places esta temporalmente no disponible.",
            }
        return {
            "status": "error",
            "error_code": f"http_{response.status_code}",
            "message": "Google Places rechazo la solicitud de prueba.",
        }

    async def get_place_details(self, place_id: str) -> dict | None:
        """Enterprise-tier fetch: only call this for candidates that passed
        the relevance pre-filter."""
        fields = _PRO_FIELDS + _ENTERPRISE_ONLY_FIELDS
        async with httpx.AsyncClient(
            timeout=15, trust_env=False, transport=self.transport
        ) as client:
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
    administrative_comuna = None
    locality = None
    for comp in address_components:
        types = comp.get("types", [])
        text = comp.get("longText")
        if "administrative_area_level_1" in types:
            region = text
        if "administrative_area_level_3" in types and not administrative_comuna:
            administrative_comuna = text
        if "locality" in types and not locality:
            locality = text
    # In Chile the locality may say "Santiago" even when the actual comuna is
    # Estacion Central, Quinta Normal, etc. The administrative level wins.
    return region, administrative_comuna or locality
