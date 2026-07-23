import httpx
import pytest

from app.enrichment.web_scraper import (
    ResponseTooLarge,
    RobotsDenied,
    SecureWebClient,
    UnsafeTarget,
    enrich_from_website,
)


async def public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://10.0.0.1", "http://[::1]"])
async def test_private_network_targets_are_blocked(url) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        del port
        return [host]

    client = SecureWebClient(resolver=resolver, min_host_interval=0)
    with pytest.raises(UnsafeTarget):
        await client.validate_url(url)


@pytest.mark.asyncio
async def test_private_redirect_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    async def resolver(host: str, port: int) -> list[str]:
        del port
        return ["127.0.0.1"] if host == "127.0.0.1" else ["93.184.216.34"]

    client = SecureWebClient(
        resolver=resolver, transport=httpx.MockTransport(handler), min_host_interval=0
    )
    with pytest.raises(UnsafeTarget):
        await client.fetch_html("https://example.com")


@pytest.mark.asyncio
async def test_validated_dns_address_is_pinned_against_rebinding() -> None:
    resolver_calls = 0
    requests: list[httpx.Request] = []

    async def flipping_resolver(host: str, port: int) -> list[str]:
        nonlocal resolver_calls
        del host, port
        resolver_calls += 1
        return ["93.184.216.34"] if resolver_calls == 1 else ["127.0.0.1"]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text="safe")

    client = SecureWebClient(
        resolver=flipping_resolver,
        transport=httpx.MockTransport(handler),
        min_host_interval=0,
    )

    await client.fetch_html("https://example.com/contact")

    assert resolver_calls == 1
    assert {request.url.host for request in requests} == {"93.184.216.34"}
    assert {request.headers["Host"] for request in requests} == {"example.com"}


@pytest.mark.asyncio
async def test_robots_disallow_is_respected() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(200, text="should not be fetched")

    client = SecureWebClient(
        resolver=public_resolver, transport=httpx.MockTransport(handler), min_host_interval=0
    )
    with pytest.raises(RobotsDenied):
        await client.fetch_html("https://example.com/private")
    assert calls == ["/robots.txt"]


@pytest.mark.asyncio
async def test_response_size_is_limited_before_download() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Length": "1000"}, content=b"x")

    client = SecureWebClient(
        max_bytes=10,
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
        min_host_interval=0,
    )
    with pytest.raises(ResponseTooLarge):
        await client.fetch_html("https://example.com")


@pytest.mark.asyncio
async def test_enrichment_extracts_contact_without_returning_page_content() -> None:
    html = b'<html><body>ventas@example.com<a href="https://instagram.com/example">IG</a></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=html)

    client = SecureWebClient(
        resolver=public_resolver, transport=httpx.MockTransport(handler), min_host_interval=0
    )
    result = await enrich_from_website("https://example.com", client=client)
    assert result["email"] == "ventas@example.com"
    assert result["social_media"] == {"instagram": "https://instagram.com/example"}
    assert result["source_url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_enrichment_reads_structured_identity_and_address_from_contact_page() -> None:
    homepage = b'<a href="/contacto">Contacto</a>'
    contact = """
    <script type="application/ld+json">
    {
      "@type": "HVACBusiness",
      "name": "Clima Andes SpA",
      "email": "ventas@climaandes.cl",
      "telephone": "+56 9 8765 4321",
      "address": {
        "@type": "PostalAddress",
        "streetAddress": "Av. Apoquindo 123",
        "addressLocality": "Las Condes",
        "addressRegion": "Región Metropolitana de Santiago"
      }
    }
    </script>
    """.encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        content = contact if request.url.path == "/contacto" else homepage
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=content)

    client = SecureWebClient(
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
        min_host_interval=0,
    )

    result = await enrich_from_website("https://climaandes.cl", client=client)

    assert result["name"] == "Clima Andes SpA"
    assert result["email"] == "ventas@climaandes.cl"
    assert result["phone"] == "+56987654321"
    assert result["whatsapp_number"] == "+56987654321"
    assert result["locations"] == [
        {
            "address": "Av. Apoquindo 123",
            "comuna_name": "Las Condes",
            "region_name": "Región Metropolitana de Santiago",
            "source_url": "https://climaandes.cl/contacto",
        }
    ]


@pytest.mark.asyncio
async def test_enrichment_crawls_bounded_service_pages_and_extracts_brands() -> None:
    homepage = b'<a href="/servicios">Servicios</a><a href="/nosotros">Nosotros</a>'
    services = "Instalacion y mantencion de aire acondicionado Daikin y Carrier".encode()
    about = "Especialistas en refrigeracion y ventilacion industrial".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        content = services if request.url.path == "/servicios" else about if request.url.path == "/nosotros" else homepage
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=content)

    client = SecureWebClient(resolver=public_resolver, transport=httpx.MockTransport(handler), min_host_interval=0)
    result = await enrich_from_website("https://empresa-hvac.cl", client=client)

    assert {"aire acondicionado", "mantencion", "instalacion", "refrigeracion", "ventilacion"} <= set(result["specialties"])
    assert {"Daikin", "Carrier"} <= set(result["brands"])
    assert len(result["pages_visited"]) == 3


@pytest.mark.asyncio
async def test_enrichment_prioritizes_contact_about_footer_and_whatsapp() -> None:
    homepage = """
    <footer>
      <span>ventas@climasur.cl</span>
      <span>+56 9 1234 5678</span>
      <a href="/productos">Productos</a>
      <a href="/contacto">Contacto</a>
      <a href="/quienes-somos">Quiénes somos</a>
    </footer>
    """.encode()
    contact = b'<a href="https://api.whatsapp.com/send?phone=56987654321">WhatsApp</a>'
    about = """
    <main><p>Somos una empresa chilena especializada en climatización, refrigeración y mantenimiento para instalaciones comerciales e industriales.</p></main>
    """.encode()
    products = b"Catalogo general"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        content = {
            "/contacto": contact,
            "/quienes-somos": about,
            "/productos": products,
        }.get(request.url.path, homepage)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=content)

    client = SecureWebClient(
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
        min_host_interval=0,
    )
    result = await enrich_from_website("https://climasur.cl", client=client)

    assert result["email"] == "ventas@climasur.cl"
    assert result["phone"] == "+56987654321"
    assert result["whatsapp_number"] == "+56987654321"
    assert result["social_media"]["whatsapp"].startswith("https://api.whatsapp.com/")
    assert "empresa chilena especializada" in result["description"]
    assert result["field_sources"]["email"] == "https://climasur.cl/"
    assert result["field_sources"]["description"] == "https://climasur.cl/quienes-somos"
    assert result["pages_visited"][:3] == [
        "https://climasur.cl/",
        "https://climasur.cl/contacto",
        "https://climasur.cl/quienes-somos",
    ]
