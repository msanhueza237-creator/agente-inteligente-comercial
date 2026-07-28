import httpx
from pydantic import SecretStr

from app.config import Settings
from app.integrations.facto import FactoClient
from app.integrations.tiendanube import TiendanubeClient


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
    assert captured[0].url.path.endswith("/v1/123/products")
    assert captured[0].headers["Authentication"] == "bearer token"
    assert not hasattr(client, "post")
