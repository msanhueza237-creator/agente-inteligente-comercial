from types import SimpleNamespace

import httpx
import pytest

from app.enrichment import paginas_amarillas
from app.enrichment.google_places import GooglePlacesClient, GooglePlacesError
from app.prospecting.contracts import (
    ProspectCandidate,
    ProspectLocation,
    ProspectingCampaign,
    ProspectingRunSnapshot,
    SourceName,
    Territory,
)
from app.prospecting.sources import AuthorizedSourceExecutor
from app.prospecting.scoring import classify_and_score
from app.prospecting.store import WorkerTask, scope_candidate_locations
from app.prospecting.validation import (
    sanitize_unsubstantiated_external_fields,
    validate_candidate,
)


@pytest.mark.asyncio
async def test_google_places_ignores_proxy_env_and_never_echoes_error_body(monkeypatch) -> None:
    seen = {}

    class FakeResponse:
        status_code = 500
        text = "X-Goog-Api-Key=secret-key ventas@example.com"

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            del args, kwargs
            return FakeResponse()

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr("app.enrichment.google_places.httpx.AsyncClient", FakeClient)
    client = GooglePlacesClient(api_key="secret-key")

    with pytest.raises(GooglePlacesError) as error:
        await client.text_search("climatización Santiago")

    assert seen["trust_env"] is False
    assert "secret-key" not in str(error.value)
    assert "ventas@example.com" not in str(error.value)


@pytest.mark.asyncio
async def test_amarillas_is_hard_disabled_before_any_network_request() -> None:
    with pytest.raises(paginas_amarillas.AmarillasDisabledError, match="official API or feed"):
        await paginas_amarillas.search("climatizacion", comuna="Santiago")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amarillas.cl/empresa",
        "https://maps.google.com/example",
        "https://instagram.com/climaandes",
        "https://www.mercadolibre.cl/listado/climatizacion",
    ],
)
def test_directory_or_platform_is_not_treated_as_official_website(url) -> None:
    candidate = ProspectCandidate(
        name="Clima Andes",
        website=url,
        location=ProspectLocation(comuna_name="Santiago"),
    )
    assert not AuthorizedSourceExecutor._is_official_website_candidate(candidate)


def test_name_affinity_allows_probable_company_website() -> None:
    candidate = ProspectCandidate(
        name="Climatización Andes SpA",
        website="https://www.clima-andes.cl",
        location=ProspectLocation(comuna_name="Santiago"),
    )
    assert AuthorizedSourceExecutor._is_official_website_candidate(candidate)


@pytest.mark.asyncio
async def test_google_details_are_requested_only_after_hvac_and_geo_prefilter(monkeypatch) -> None:
    address = [
        {"types": ["administrative_area_level_1"], "longText": "Región Metropolitana de Santiago"},
        {"types": ["administrative_area_level_3"], "longText": "Santiago"},
    ]

    class FakeGoogle:
        details_calls = []

        async def text_search(self, query, *, max_results):
            del query, max_results
            return [
                {
                    "id": "valid",
                    "displayName": {"text": "Servicios Andes"},
                    "types": ["air_conditioning_contractor"],
                    "addressComponents": address,
                },
                {
                    "id": "irrelevant",
                    "displayName": {"text": "Restaurante Andes"},
                    "types": ["restaurant"],
                    "addressComponents": address,
                },
                {
                    "id": "outside",
                    "displayName": {"text": "Climatización Costa"},
                    "types": ["air_conditioning_contractor"],
                    "addressComponents": [
                        {"types": ["administrative_area_level_1"], "longText": "Valparaíso"},
                        {"types": ["administrative_area_level_3"], "longText": "Valparaíso"},
                    ],
                },
            ]

        async def get_place_details(self, place_id):
            self.details_calls.append(place_id)
            return {
                "id": place_id,
                "displayName": {"text": "Servicios Andes"},
                "types": ["air_conditioning_contractor"],
                "addressComponents": address,
                "formattedAddress": "Av. Libertador 100, Santiago",
                "websiteUri": "https://servicios-andes.cl",
            }

    monkeypatch.setattr("app.prospecting.sources.GooglePlacesClient", FakeGoogle)

    async def structured_website(_website):
        return {
            "name": "Servicios Andes",
            "email": "ventas@servicios-andes.cl",
            "source_url": "https://servicios-andes.cl/contacto",
            "locations": [
                {
                    "address": "Av. Libertador 100, Santiago",
                    "comuna_name": "Santiago",
                    "region_name": "Región Metropolitana de Santiago",
                    "source_url": "https://servicios-andes.cl/contacto",
                }
            ],
        }

    monkeypatch.setattr("app.prospecting.sources.enrich_from_website", structured_website)
    executor = AuthorizedSourceExecutor()
    task = WorkerTask(
        id="task",
        run_id="run",
        source=SourceName.google_places,
        keyword="climatización",
        region_code="13",
        region_name="Metropolitana de Santiago",
        comuna_code="13101",
        comuna_name="Santiago",
        max_results=20,
        attempt_count=1,
        max_attempts=3,
    )
    snapshot = ProspectingRunSnapshot(
        crm_run_id="run",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign",
            name="Google",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13101",
                    comuna_name="Santiago",
                ),
            ),
            keywords=("climatización",),
            sources=(SourceName.google_places, SourceName.official_website),
        ),
    )

    candidates = await executor.search(task, snapshot)

    assert len(candidates) == 1
    assert FakeGoogle.details_calls == ["valid"]
    prepared = scope_candidate_locations(
        sanitize_unsubstantiated_external_fields(classify_and_score(candidates[0], snapshot)),
        snapshot,
    )
    assert validate_candidate(prepared, snapshot).accepted
    assert prepared.email == "ventas@servicios-andes.cl"
    assert prepared.import_eligible
    assert prepared.importable_location_indexes == (0,)
    permanent_fields = {
        evidence.field
        for evidence in prepared.evidence
        if evidence.provider == SourceName.official_website
    }
    assert {
        "name",
        "email",
        "website",
        "locations[0].region_code",
        "locations[0].comuna_code",
        "locations[0].address",
    }.issubset(permanent_fields)


@pytest.mark.asyncio
async def test_email_only_official_enrichment_is_visible_but_not_importable(monkeypatch) -> None:
    task = WorkerTask(
        id="task-email-only",
        run_id="run-email-only",
        source=SourceName.google_places,
        keyword="climatización",
        region_code="13",
        region_name="Metropolitana de Santiago",
        comuna_code="13101",
        comuna_name="Santiago",
        max_results=20,
        attempt_count=1,
        max_attempts=3,
    )
    snapshot = ProspectingRunSnapshot(
        crm_run_id="run-email-only",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign-email-only",
            name="Email only",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13101",
                    comuna_name="Santiago",
                ),
            ),
            keywords=("climatización",),
            sources=(SourceName.google_places, SourceName.official_website),
        ),
    )
    candidate = ProspectCandidate(
        name="Climatización Temporal SpA",
        website="https://clima-temporal.cl",
        location=ProspectLocation(
            region_code="13",
            region_name="Región Metropolitana de Santiago",
            comuna_code="13101",
            comuna_name="Santiago",
        ),
    )

    async def email_only(_website):
        return {
            "email": "ventas@clima-temporal.cl",
            "source_url": "https://clima-temporal.cl/contacto",
        }

    monkeypatch.setattr("app.prospecting.sources.enrich_from_website", email_only)
    executor = AuthorizedSourceExecutor()

    enriched = await executor._enrich_official_website(candidate, task)
    prepared = scope_candidate_locations(enriched, snapshot)

    assert prepared.email == "ventas@clima-temporal.cl"
    assert not prepared.import_eligible
    assert "insufficient_permanent_evidence" in prepared.review_flags


@pytest.mark.asyncio
async def test_brave_service_area_mention_does_not_prove_business_domicile(monkeypatch) -> None:
    payload = {
        "web": {
            "results": [
                {
                    "title": "Clima Costa - servicio en Las Condes",
                    "description": "Climatización y refrigeración para toda Las Condes",
                    "url": "https://climacosta.cl",
                }
            ]
        }
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.prospecting.sources.httpx.AsyncClient", client_factory)
    executor = AuthorizedSourceExecutor()
    executor.settings = SimpleNamespace(brave_search_api_key="test")
    task = WorkerTask(
        id="task-brave-coverage",
        run_id="run-brave-coverage",
        source=SourceName.brave_search,
        keyword="climatización",
        region_code="13",
        region_name="Metropolitana de Santiago",
        comuna_code="13114",
        comuna_name="Las Condes",
        max_results=20,
        attempt_count=1,
        max_attempts=3,
    )
    snapshot = ProspectingRunSnapshot(
        crm_run_id="run-brave-coverage",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign-brave-coverage",
            name="Brave coverage",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13114",
                    comuna_name="Las Condes",
                ),
            ),
            keywords=("climatización",),
            sources=(SourceName.brave_search, SourceName.official_website),
        ),
    )

    candidates = await executor._brave(task)
    prepared = sanitize_unsubstantiated_external_fields(
        classify_and_score(candidates[0], snapshot)
    )

    assert prepared.location.region_code is None
    assert prepared.location.comuna_code is None
    assert not any(evidence.field.startswith("location.") for evidence in prepared.evidence)
    assert "outside_requested_territory" in validate_candidate(prepared, snapshot).reasons
