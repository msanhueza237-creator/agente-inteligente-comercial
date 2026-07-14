import json
import uuid

import httpx
import pytest

from app.crm.http import CRMTransportError, HttpCRMPort, business_payload_hash
from app.crm.port import (
    CRMIdempotencyConflict,
    CRMLeaseLostError,
    CRMPermanentError,
    CRMRetryableError,
)
from app.prospecting.contracts import (
    CompletionReport,
    ProspectCandidate,
    ProspectLocation,
    ProspectingCampaign,
    ProspectingRunSnapshot,
    RunStatus,
    SourceName,
    Territory,
)


RUN_ID = "0d12d64a-99fd-4d71-872a-8c49e65f1885"
CAMPAIGN_ID = "15e1ded2-bcb7-4f13-9d6c-a2f29bb50776"
USER_ID = "0e9281f1-2257-473d-8f8e-2c1bd8fa98d4"


def snapshot_payload() -> dict:
    snapshot = ProspectingRunSnapshot(
        crm_run_id=RUN_ID,
        campaign_version=1,
        requested_by=USER_ID,
        campaign=ProspectingCampaign(
            crm_campaign_id=CAMPAIGN_ID,
            name="HTTP",
            territories=(
                Territory(
                    region_code="13",
                    region_name="Metropolitana de Santiago",
                    comuna_code="13101",
                    comuna_name="Santiago",
                ),
            ),
            keywords=("climatización",),
            sources=(SourceName.brave_search, SourceName.official_website),
            target_types=("tecnico", "instalador grande"),
        ),
    )
    return snapshot.model_dump(mode="json")


@pytest.mark.asyncio
async def test_http_port_claim_uses_api_header_and_parses_shared_tasks() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(
            200,
            json={
                "data": {
                    "snapshot": snapshot_payload(),
                    "lease_token": "lease-secret",
                    "lease_expires_at": "2026-07-13T18:00:00Z",
                    "tasks": [
                        {
                            "id": "aa463b6b-d8ce-4f56-b412-2eff44f624c3",
                            "run_id": RUN_ID,
                            "source": "brave_search",
                            "keyword": "climatización",
                            "region_code": "13",
                            "region_name": "Metropolitana de Santiago",
                            "comuna_code": "13101",
                            "comuna_name": "Santiago",
                            "max_results": 20,
                            "max_attempts": 3,
                        }
                    ],
                }
            },
        )

    port = HttpCRMPort(
        base_url="https://crm.test",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )
    claim = await port.claim_run("worker-1")
    assert claim is not None
    assert claim.tasks[0].task_id == "aa463b6b-d8ce-4f56-b412-2eff44f624c3"
    assert claim.snapshot.campaign.target_types == ("tecnico", "instalador grande")
    assert seen["request"].headers["X-Climactiva-Api-Key"] == "ca_live_test"
    assert seen["request"].headers.get("Idempotency-Key")
    assert seen["request"].url.path == "/crm-agent/prospecting-runs/claim"


@pytest.mark.asyncio
async def test_http_port_sends_lease_and_idempotency_headers() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"run": None})
        return httpx.Response(202, json={"ok": True})

    port = HttpCRMPort(
        base_url="https://crm.test/crm-agent",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )
    await port.claim_run("worker-http")
    await port.upsert_candidates(
        RUN_ID,
        "lease-1",
        [ProspectCandidate(name="Clima", location=ProspectLocation(comuna_name="Santiago"))],
        "idem-1",
    )
    request = seen["request"]
    assert request.url.path == f"/crm-agent/prospecting-runs/{RUN_ID}/candidates/batch"
    assert request.headers["Idempotency-Key"] == "idem-1"
    assert b'"lease_token":"lease-1"' in request.content


@pytest.mark.asyncio
async def test_http_port_does_not_echo_upstream_body_in_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"run": None})
        return httpx.Response(500, text="api_key=secret-value contacto@example.com")

    port = HttpCRMPort(
        base_url="https://crm.test/crm-agent",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )
    await port.claim_run("worker-http")
    with pytest.raises(CRMTransportError) as error:
        await port.heartbeat(RUN_ID, "lease-1")
    assert "secret-value" not in str(error.value)
    assert "contacto@example.com" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 410, 423])
async def test_http_heartbeat_treats_auth_or_lease_rejection_as_lost(status) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"run": None})
        return httpx.Response(status)

    port = HttpCRMPort(
        base_url="https://crm.test",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )
    await port.claim_run("worker-http")
    assert not await port.heartbeat(RUN_ID, "lease-1")


@pytest.mark.asyncio
async def test_http_heartbeat_propagates_cancel_requested() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim"):
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={"data": {"ok": True, "cancel_requested": True}},
        )

    port = HttpCRMPort(
        base_url="https://crm.test",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )
    await port.claim_run("worker-http")

    heartbeat = await port.heartbeat(RUN_ID, "lease-1")

    assert heartbeat.lease_valid
    assert heartbeat.cancel_requested


@pytest.mark.asyncio
async def test_http_heartbeat_surfaces_idempotency_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim"):
            return httpx.Response(204)
        return httpx.Response(409)

    port = HttpCRMPort(
        base_url="https://crm.test",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )
    await port.claim_run("worker-http")
    with pytest.raises(CRMIdempotencyConflict):
        await port.heartbeat(RUN_ID, "lease-1")


@pytest.mark.asyncio
async def test_http_outbox_business_payload_stays_equal_with_a_new_lease() -> None:
    claim_number = 0
    candidate_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal claim_number
        if request.url.path.endswith("/claim"):
            claim_number += 1
            return httpx.Response(
                200,
                json={
                    "snapshot": snapshot_payload(),
                    "lease_token": f"lease-{claim_number}",
                    "lease_expires_at": "2026-07-13T18:00:00Z",
                    "tasks": [],
                },
            )
        candidate_requests.append(request)
        return httpx.Response(
            202,
            json={"accepted": 1, "rejected_limit": 1, "candidates_found": 1000},
        )

    port = HttpCRMPort(
        base_url="https://crm.test",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )
    candidate = ProspectCandidate(
        candidate_id=str(uuid.UUID("bbfb2e5a-8085-4bc5-8efd-163dbb4db765")),
        name="Clima HTTP",
        location=ProspectLocation(comuna_code="13101", comuna_name="Santiago"),
    )
    first = await port.claim_run("worker-http")
    await port.upsert_candidates(RUN_ID, first.lease_token, [candidate], "business-key-http")
    second = await port.claim_run("worker-http")
    ack = await port.upsert_candidates(
        RUN_ID, second.lease_token, [candidate], "business-key-http"
    )

    first_body = json.loads(candidate_requests[0].content)
    second_body = json.loads(candidate_requests[1].content)
    assert first_body["candidates"] == second_body["candidates"]
    assert first_body["lease_token"] != second_body["lease_token"]
    assert {request.headers["Idempotency-Key"] for request in candidate_requests} == {
        "business-key-http"
    }
    assert business_payload_hash(first_body) == business_payload_hash(second_body)
    assert ack.accepted == 1
    assert ack.rejected_limit == 1
    assert ack.candidates_found == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, CRMPermanentError),
        (422, CRMPermanentError),
        (403, CRMLeaseLostError),
        (409, CRMIdempotencyConflict),
        (425, CRMRetryableError),
        (429, CRMRetryableError),
        (500, CRMRetryableError),
    ],
)
async def test_http_write_classifies_response_status(status, error_type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim"):
            return httpx.Response(204)
        return httpx.Response(status, text="never echo this body")

    port = HttpCRMPort(
        base_url="https://crm.test",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )
    await port.claim_run("worker-http")
    candidate = ProspectCandidate(
        candidate_id="bbfb2e5a-8085-4bc5-8efd-163dbb4db765",
        name="Clima HTTP",
        location=ProspectLocation(region_code="13", comuna_code="13101"),
    )

    with pytest.raises(error_type):
        await port.upsert_candidates(RUN_ID, "lease-1", [candidate], "status-key")


@pytest.mark.asyncio
async def test_http_replays_same_idempotent_business_payload_after_response_loss() -> None:
    candidate_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim"):
            return httpx.Response(204)
        candidate_requests.append(request)
        if len(candidate_requests) == 1:
            # The CRM may have committed before the connection dropped.
            raise httpx.ReadError("response lost", request=request)
        return httpx.Response(202, json={"accepted": 1})

    port = HttpCRMPort(
        base_url="https://crm.test",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )
    await port.claim_run("worker-http")
    candidate = ProspectCandidate(
        candidate_id="bbfb2e5a-8085-4bc5-8efd-163dbb4db765",
        name="Clima HTTP",
        location=ProspectLocation(region_code="13", comuna_code="13101"),
    )

    with pytest.raises(CRMRetryableError):
        await port.upsert_candidates(RUN_ID, "lease-1", [candidate], "response-lost-key")
    await port.upsert_candidates(RUN_ID, "lease-2", [candidate], "response-lost-key")

    first_body = json.loads(candidate_requests[0].content)
    second_body = json.loads(candidate_requests[1].content)
    assert business_payload_hash(first_body) == business_payload_hash(second_body)
    assert first_body["lease_token"] != second_body["lease_token"]
    assert {
        request.headers["Idempotency-Key"] for request in candidate_requests
    } == {"response-lost-key"}


@pytest.mark.asyncio
async def test_terminal_replay_uses_persisted_worker_without_a_new_claim() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(202, json={"ok": True})

    port = HttpCRMPort(
        base_url="https://crm.test",
        api_key="ca_live_test",
        transport=httpx.MockTransport(handler),
    )

    await port.complete_run(
        RUN_ID,
        "expired-original-lease",
        CompletionReport(status=RunStatus.completed, stats={}),
        "terminal-idempotency-key",
        worker_id="worker-original",
    )

    assert seen["worker_id"] == "worker-original"
    assert seen["lease_token"] == "expired-original-lease"
