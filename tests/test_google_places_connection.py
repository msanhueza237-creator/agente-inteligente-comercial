import json

import httpx
import pytest

from app.enrichment.google_places import GooglePlacesClient


@pytest.mark.asyncio
async def test_connection_check_requests_only_identifier_and_hides_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"places": [{"id": "place-1"}]})

    client = GooglePlacesClient(
        "secret-google-key",
        transport=httpx.MockTransport(handler),
    )
    result = await client.check_connection()

    request = seen["request"]
    assert isinstance(request, httpx.Request)
    assert result["status"] == "connected"
    assert request.headers["X-Goog-FieldMask"] == "places.id"
    assert json.loads(request.content)["pageSize"] == 1
    assert "secret-google-key" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "status", "error_code"),
    [
        (403, "error", "forbidden"),
        (429, "quota_exhausted", "quota_exhausted"),
        (503, "error", "provider_unavailable"),
    ],
)
async def test_connection_check_returns_safe_status(
    http_status: int, status: str, error_code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(http_status, text="secret provider response")

    client = GooglePlacesClient("secret", transport=httpx.MockTransport(handler))
    result = await client.check_connection()

    assert result["status"] == status
    assert result["error_code"] == error_code
    assert "secret provider response" not in str(result)
