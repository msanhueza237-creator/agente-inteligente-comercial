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
from app.prospecting.sources import AuthorizedSourceExecutor, build_company_summary, build_google_query_plan
from app.prospecting.scoring import classify_and_score
from app.prospecting.store import WorkerTask, scope_candidate_locations
from app.prospecting.validation import (
    sanitize_unsubstantiated_external_fields,
    validate_candidate,
)


def test_company_summary_uses_evidence_and_marks_unknown_activity() -> None:
    detailed = ProspectCandidate(
        name="Clima Andes",
        location=ProspectLocation(comuna_name="Santiago"),
        specialties=("instalacion", "mantencion", "aire acondicionado"),
        brands=("Daikin", "Midea"),
        website="https://climaandes.cl",
        phone="+56912345678",
    )
    summary = build_company_summary(detailed)
    assert "instalación" in summary
    assert "Daikin y Midea" in summary
    assert "Santiago" in summary

    unknown = ProspectCandidate(
        name="Nombre poco descriptivo",
        location=ProspectLocation(comuna_name="Maipú"),
    )
    assert "No fue posible confirmar públicamente su actividad específica" in build_company_summary(unknown)


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
async def test_google_expansion_deduplicates_and_keeps_generic_geo_matches(monkeypatch) -> None:
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

    assert len(candidates) == 2
    assert FakeGoogle.details_calls == ["valid", "irrelevant"]
    assert candidates.metrics == {
        "queries_planned": 6,
        "queries_executed": 6,
        "raw_results": 18,
        "unique_results": 3,
        "outside_territory_discovery": 1,
        "details_requested": 2,
        "candidates_prepared": 2,
        "estimated_google_cost_usd": 0.232,
        "budget_limited": False,
        "budget_reason": None,
        "budget_alert": False,
        "run_spend_usd": 0.232,
        "daily_spend_usd": 0.232,
        "monthly_spend_usd": 0.232,
        "run_budget_usd": 10.0,
    }
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


def test_google_query_plan_expands_target_intents_without_duplicates() -> None:
    task = WorkerTask(
        id="task-plan",
        run_id="run-plan",
        source=SourceName.google_places,
        keyword="aire acondicionado",
        region_code="13",
        region_name="Metropolitana de Santiago",
        comuna_code="13101",
        comuna_name="Santiago",
        max_results=20,
        attempt_count=1,
        max_attempts=3,
    )
    snapshot = ProspectingRunSnapshot(
        crm_run_id="run-plan",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign-plan",
            name="Masiva",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13101",
                    comuna_name="Santiago",
                ),
            ),
            keywords=("aire acondicionado",),
            sources=(SourceName.google_places,),
            target_types=("tienda comercial", "tecnico"),
        ),
    )

    queries = build_google_query_plan(task, snapshot, max_queries=6)

    assert len(queries) == 6
    assert len({query.casefold() for query in queries}) == 6
    assert queries[0].startswith("aire acondicionado en Santiago")
    assert any("tienda de aire acondicionado" in query for query in queries)
    assert any("servicio tecnico de aire acondicionado" in query for query in queries)


def test_market_coverage_query_plan_interleaves_commercial_roles() -> None:
    task = WorkerTask(
        id="task-market",
        run_id="run-market",
        source=SourceName.google_places,
        keyword="refrigeracion comercial",
        region_code="13",
        region_name="Metropolitana de Santiago",
        comuna_code="13101",
        comuna_name="Santiago",
        max_results=20,
        attempt_count=1,
        max_attempts=3,
    )
    snapshot = ProspectingRunSnapshot(
        crm_run_id="run-market",
        campaign_version=1,
        requested_by="admin",
        campaign=ProspectingCampaign(
            crm_campaign_id="campaign-market",
            name="Cobertura de mercado",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13101",
                    comuna_name="Santiago",
                ),
            ),
            keywords=("refrigeracion comercial",),
            sources=(SourceName.google_places,),
            target_types=("distribuidor", "tienda comercial", "competencia"),
        ),
    )

    queries = build_google_query_plan(task, snapshot, max_queries=6)

    assert any("distribuidor de refrigeracion comercial" in query for query in queries)
    assert any("tienda de refrigeracion comercial" in query for query in queries)
    assert any("empresa de refrigeracion comercial" in query for query in queries)
    assert any("mayorista de refrigeracion comercial" in query for query in queries)
    assert any("repuestos de refrigeracion comercial" in query for query in queries)


def test_market_radar_uses_broad_commercial_intents_without_company_names() -> None:
    from app.prospecting.sources import build_brave_market_query_plan

    task = WorkerTask(
        id="radar", run_id="run", source=SourceName.brave_search,
        keyword="refrigeracion comercial", region_code="13",
        region_name="Metropolitana de Santiago", comuna_code="13101",
        comuna_name="Santiago", max_results=20, attempt_count=1, max_attempts=3,
    )
    queries = build_brave_market_query_plan(task, max_queries=8)

    assert len(queries) == 8
    assert any("distribuidor" in query for query in queries)
    assert any("mayorista" in query for query in queries)
    assert any("importador" in query for query in queries)
    assert any("marcas catalogo" in query for query in queries)
    assert not any("acondipart" in query.casefold() for query in queries)


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
            "description": "Empresa dedicada a la instalación y mantenimiento de climatización comercial.",
            "source_url": "https://clima-temporal.cl/contacto",
            "field_sources": {
                "email": "https://clima-temporal.cl/contacto",
                "description": "https://clima-temporal.cl/nosotros",
            },
        }

    monkeypatch.setattr("app.prospecting.sources.enrich_from_website", email_only)
    executor = AuthorizedSourceExecutor()

    enriched = await executor._enrich_official_website(candidate, task)
    prepared = scope_candidate_locations(enriched, snapshot)

    assert prepared.email == "ventas@clima-temporal.cl"
    assert prepared.description.startswith("Empresa dedicada")
    assert any(
        evidence.field == "description"
        and evidence.source_url == "https://clima-temporal.cl/nosotros"
        for evidence in prepared.evidence
    )
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

    candidates = await executor._brave(task, snapshot)
    prepared = sanitize_unsubstantiated_external_fields(classify_and_score(candidates[0], snapshot))

    assert prepared.location.region_code is None
    assert prepared.location.comuna_code is None
    assert not any(evidence.field.startswith("location.") for evidence in prepared.evidence)
    assert "outside_requested_territory" in validate_candidate(prepared, snapshot).reasons


@pytest.mark.asyncio
async def test_post_enrichment_never_uses_brave_to_find_a_missing_site(monkeypatch) -> None:
    executor = AuthorizedSourceExecutor()
    executor.settings = SimpleNamespace(brave_search_api_key="configured")
    candidate = ProspectCandidate(
        name="Empresa sin sitio",
        location=ProspectLocation(region_code="13", region_name="Metropolitana de Santiago", comuna_code="13101", comuna_name="Santiago"),
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("post enrichment must not call Brave")

    monkeypatch.setattr(executor, "_find_official_website", forbidden)
    enriched, summary = await executor.enrich_existing(candidate, "run-no-brave")

    assert "official_site_missing" in enriched.review_flags
    assert summary["brave_queries_used"] == 0
    assert summary["official_site_missing"] is True
