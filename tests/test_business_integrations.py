import httpx
from pydantic import SecretStr

from app.config import Settings
from app.integrations.facto import FactoClient
from app.integrations.tiendanube import TiendanubeClient, TiendanubeError


def settings_for_tests(**changes) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost/test",
        **changes,
    )


async def test_facto_authenticates_and_reads_products_without_write_methods() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"access_token": "temporary-token"})
        return httpx.Response(200, json={"data": [{"id": 1, "name": "Producto"}]})

    client = FactoClient(
        settings_for_tests(
            facto_enabled=True,
            facto_client_id=SecretStr("id"),
            facto_client_secret=SecretStr("secret"),
            facto_username=SecretStr("user"),
            facto_password=SecretStr("password"),
        ),
        transport=httpx.MockTransport(handler),
    )
    result = await client.products()
    assert result["data"][0]["id"] == 1
    assert [request.method for request in requests] == ["POST", "GET"]
    assert not hasattr(client, "post")


async def test_facto_reads_product_and_document_details() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"access_token": "temporary-token"})
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    client = FactoClient(
        settings_for_tests(
            facto_enabled=True,
            facto_client_id=SecretStr("id"),
            facto_client_secret=SecretStr("secret"),
            facto_username=SecretStr("user"),
            facto_password=SecretStr("password"),
        ),
        transport=httpx.MockTransport(handler),
    )

    await client.product(40)
    await client.document(100)
    await client.inbox_documents(page=2, per_page=50)

    assert paths[-3].endswith("/products/40")
    assert paths[-2].endswith("/documents/100")
    assert paths[-1].endswith("/inbox_documents")


async def test_tiendanube_uses_bearer_header_and_store_path() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[{"id": 10}])

    client = TiendanubeClient(
        settings_for_tests(
            tiendanube_enabled=True,
            tiendanube_store_id="123",
            tiendanube_access_token=SecretStr("token"),
        ),
        transport=httpx.MockTransport(handler),
    )
    result = await client.products(per_page=1)
    assert result[0]["id"] == 10
    assert captured[0].url.path.endswith("/2025-03/123/products")
    assert captured[0].headers["Authorization"] == "Bearer token"
    assert captured[0].headers["Authentication"] == "bearer token"
    assert not hasattr(client, "post")


async def test_tiendanube_falls_back_to_legacy_v1_authentication_header() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/2025-03/123/products"):
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(200, json=[{"id": 20}])

    client = TiendanubeClient(
        settings_for_tests(
            tiendanube_enabled=True,
            tiendanube_store_id="123",
            tiendanube_access_token=SecretStr("token"),
        ),
        transport=httpx.MockTransport(handler),
    )

    result = await client.products(per_page=1)

    assert result[0]["id"] == 20
    assert len(captured) == 2
    assert captured[1].url.path.endswith("/v1/123/products")
    assert captured[1].headers["Authentication"] == "bearer token"


async def test_tiendanube_error_includes_provider_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "invalid_token", "message": "Token is invalid"},
        )

    client = TiendanubeClient(
        settings_for_tests(
            tiendanube_enabled=True,
            tiendanube_store_id="123",
            tiendanube_access_token=SecretStr("token"),
        ),
        transport=httpx.MockTransport(handler),
    )

    try:
        await client.products(per_page=1)
    except TiendanubeError as exc:
        assert "401" in str(exc)
        assert "Token is invalid" in str(exc)
    else:
        raise AssertionError("Expected TiendanubeError")
