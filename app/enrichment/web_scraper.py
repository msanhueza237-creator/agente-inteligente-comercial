"""Conservative enrichment from a company's official public website.

Every target and redirect is DNS-checked, private/non-global addresses are
blocked, robots.txt is respected, and the response is streamed under a hard
byte limit. Failures intentionally return an empty enrichment result.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
import urllib.robotparser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+")
_SOCIAL_DOMAINS = {
    "facebook": "facebook.com",
    "instagram": "instagram.com",
    "linkedin": "linkedin.com",
    "whatsapp": "wa.me",
}
_PAGE_TOKENS = ("contact", "contacto", "ubicacion", "sucursal", "nosotros", "empresa", "servicio", "solucion", "producto", "marca")
_SPECIALTY_TERMS = (
    "aire acondicionado", "climatizacion", "refrigeracion", "ventilacion", "calefaccion",
    "mantencion", "mantenimiento", "instalacion", "servicio tecnico", "proyecto hvac",
    "camara frigorifica", "extraccion de aire", "automatizacion", "eficiencia energetica",
)
_BRAND_TERMS = (
    "Daikin", "Midea", "Carrier", "Trane", "York", "LG", "Samsung", "Mitsubishi",
    "Fujitsu", "Anwo", "Khöne", "Hisense", "Bosch", "Rheem", "Honeywell", "Danfoss",
    "Copeland", "Emerson", "Sporlan", "Sodeca", "Systemair", "Trox",
)
USER_AGENT = "ClimaActivaBot/1.0 (+business prospecting; contact: administracion@climactiva.cl)"
MAX_REDIRECTS = 3

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeTarget(ValueError):
    pass


class RobotsDenied(PermissionError):
    pass


class ResponseTooLarge(ValueError):
    pass


async def _default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({record[4][0] for record in records})


def _is_global_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


@dataclass(frozen=True)
class SafeResponse:
    final_url: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class _PinnedOrigin:
    ip: str
    hostname: str
    host_header: str


def _jsonld_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_nodes(item)
    elif isinstance(value, dict):
        yield value
        if "@graph" in value:
            yield from _jsonld_nodes(value["@graph"])


def _page_enrichment(response: SafeResponse) -> tuple[dict, list[str]]:
    soup = BeautifulSoup(response.content, "lxml")
    source_url = response.final_url
    emails = set(EMAIL_RE.findall(soup.get_text(" ")))
    phones: set[str] = set()
    names: list[str] = []
    locations: list[dict] = []
    declared_url: str | None = None

    for anchor in soup.find_all("a", href=True):
        href = urljoin(source_url, str(anchor["href"]))
        if href.lower().startswith("mailto:"):
            emails.add(href.split(":", 1)[1].split("?", 1)[0])
        elif href.lower().startswith("tel:"):
            phones.add(href.split(":", 1)[1].split("?", 1)[0])

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = json.loads(script.string or script.get_text() or "null")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _jsonld_nodes(raw):
            if isinstance(node.get("name"), str):
                names.append(node["name"].strip())
            if isinstance(node.get("email"), str):
                emails.add(node["email"].removeprefix("mailto:").strip())
            if isinstance(node.get("telephone"), str):
                phones.add(node["telephone"].strip())
            if isinstance(node.get("url"), str):
                declared_url = node["url"]
            addresses = node.get("address")
            if isinstance(addresses, dict):
                addresses = [addresses]
            if not isinstance(addresses, list):
                continue
            for address in addresses:
                if not isinstance(address, dict):
                    continue
                prepared = {
                    "address": address.get("streetAddress"),
                    "comuna_name": address.get("addressLocality"),
                    "region_name": address.get("addressRegion"),
                    "source_url": source_url,
                }
                if any(prepared.get(key) for key in ("address", "comuna_name", "region_name")):
                    locations.append(prepared)

    site_name = soup.find("meta", attrs={"property": "og:site_name"})
    if site_name and site_name.get("content"):
        names.append(str(site_name["content"]).strip())
    item_name = soup.select_one('[itemprop="name"]')
    if item_name:
        names.append(str(item_name.get("content") or item_name.get_text(" ")).strip())
    locality = soup.select_one('[itemprop="addressLocality"]')
    region = soup.select_one('[itemprop="addressRegion"]')
    street = soup.select_one('[itemprop="streetAddress"]')
    if locality or region or street:
        locations.append(
            {
                "address": (
                    str(street.get("content") or street.get_text(" ")).strip()
                    if street
                    else None
                ),
                "comuna_name": (
                    str(locality.get("content") or locality.get_text(" ")).strip()
                    if locality
                    else None
                ),
                "region_name": (
                    str(region.get("content") or region.get_text(" ")).strip()
                    if region
                    else None
                ),
                "source_url": source_url,
            }
        )

    social: dict[str, str] = {}
    relevant_urls: list[str] = []
    source_host = (urlsplit(source_url).hostname or "").lower()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(source_url, str(anchor["href"]))
        target_host = (urlsplit(href).hostname or "").lower()
        for platform, domain in _SOCIAL_DOMAINS.items():
            if platform not in social and (
                target_host == domain or target_host.endswith(f".{domain}")
            ):
                social[platform] = href
        label = f"{anchor.get_text(' ')} {urlsplit(href).path}".casefold()
        if target_host == source_host and any(token in label for token in _PAGE_TOKENS):
            clean = href.split("#", 1)[0]
            if clean not in relevant_urls:
                relevant_urls.append(clean)

    visible_text = " ".join(soup.get_text(" ").casefold().split())
    specialties = sorted({term for term in _SPECIALTY_TERMS if term in visible_text})
    brands = sorted({brand for brand in _BRAND_TERMS if re.search(rf"(?<!\w){re.escape(brand.casefold())}(?!\w)", visible_text)}, key=str.casefold)

    result = {
        "name": next((name for name in names if name), None),
        "email": sorted(email for email in emails if email)[0] if emails else None,
        "phone": sorted(phone for phone in phones if phone)[0] if phones else None,
        "website": declared_url or source_url,
        "locations": locations or None,
        "social_media": social or None,
        "specialties": specialties or None,
        "brands": brands or None,
        "pages_visited": [source_url],
        "source_url": source_url,
    }
    return result, relevant_urls


class SecureWebClient:
    _rate_lock = asyncio.Lock()
    _last_request_by_host: dict[str, float] = {}

    def __init__(
        self,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
        min_host_interval: float = 1.0,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.max_bytes = max_bytes or settings.website_max_bytes
        self.timeout = timeout or settings.website_timeout_seconds
        self.min_host_interval = min_host_interval
        self.resolver = resolver or _default_resolver
        self.transport = transport
        self._pinned_origins: dict[tuple[str, str, int], _PinnedOrigin] = {}
        self._validated_urls: dict[str, tuple[str, _PinnedOrigin]] = {}

    async def validate_url(self, raw_url: str) -> str:
        candidate = raw_url.strip()
        if not candidate:
            raise UnsafeTarget("empty URL")
        if "://" not in candidate:
            candidate = f"https://{candidate}"
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"}:
            raise UnsafeTarget("only HTTP(S) targets are allowed")
        if parsed.username or parsed.password:
            raise UnsafeTarget("URLs containing credentials are not allowed")
        if not parsed.hostname:
            raise UnsafeTarget("URL has no hostname")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            raise UnsafeTarget("local hostnames are blocked")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        origin_key = (parsed.scheme, host, port)
        pinned = self._pinned_origins.get(origin_key)
        if pinned is None:
            addresses = [host] if _is_global_ip(host) else await self.resolver(host, port)
            if not addresses or any(not _is_global_ip(address) for address in addresses):
                raise UnsafeTarget("target resolves to a private or non-global address")
            default_port = 443 if parsed.scheme == "https" else 80
            header_host = f"[{host}]" if ":" in host else host
            host_header = header_host if port == default_port else f"{header_host}:{port}"
            pinned = _PinnedOrigin(
                ip=sorted(addresses, key=lambda value: (":" in value, value))[0],
                hostname=host,
                host_header=host_header,
            )
            self._pinned_origins[origin_key] = pinned
        netloc = f"[{host}]" if ":" in host else host
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        canonical = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
        self._validated_urls[canonical] = (parsed.scheme, pinned)
        return canonical

    async def _wait_rate_limit(self, host: str) -> None:
        if self.min_host_interval <= 0:
            return
        async with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_by_host.get(host, 0.0)
            if elapsed < self.min_host_interval:
                await asyncio.sleep(self.min_host_interval - elapsed)
            self._last_request_by_host[host] = time.monotonic()

    async def _request_once(self, url: str) -> tuple[int, httpx.Headers, bytes]:
        validated = self._validated_urls.get(url)
        if validated is None:
            url = await self.validate_url(url)
            validated = self._validated_urls[url]
        scheme, pinned = validated
        await self._wait_rate_limit(pinned.hostname)
        parsed = urlsplit(url)
        ip_netloc = f"[{pinned.ip}]" if ":" in pinned.ip else pinned.ip
        if parsed.port:
            ip_netloc = f"{ip_netloc}:{parsed.port}"
        connect_url = urlunsplit(
            (scheme, ip_netloc, parsed.path or "/", parsed.query, "")
        )
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html, text/plain;q=0.8"},
            trust_env=False,
            transport=self.transport,
        ) as client:
            async with client.stream(
                "GET",
                connect_url,
                headers={"Host": pinned.host_header},
                extensions={"sni_hostname": pinned.hostname},
            ) as response:
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise ResponseTooLarge("invalid Content-Length") from exc
                    if declared_size > self.max_bytes:
                        raise ResponseTooLarge("response exceeds configured size limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ResponseTooLarge("response exceeds configured size limit")
                    chunks.append(chunk)
                return response.status_code, response.headers, b"".join(chunks)

    async def _robots_allowed(self, target_url: str) -> bool:
        parsed = urlsplit(target_url)
        robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        robots_url = await self.validate_url(robots_url)
        try:
            status, _, content = await self._request_once(robots_url)
        except (httpx.HTTPError, OSError, ResponseTooLarge):
            return False
        if status == 404:
            return True
        if status < 200 or status >= 300:
            return False
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(content.decode("utf-8", errors="replace").splitlines())
        return parser.can_fetch(USER_AGENT, target_url)

    async def fetch_html(self, raw_url: str) -> SafeResponse:
        current = await self.validate_url(raw_url)
        for redirect_number in range(MAX_REDIRECTS + 1):
            if not await self._robots_allowed(current):
                raise RobotsDenied("robots.txt does not allow this fetch")
            status, headers, content = await self._request_once(current)
            if status in {301, 302, 303, 307, 308}:
                if redirect_number >= MAX_REDIRECTS:
                    raise UnsafeTarget("too many redirects")
                location = headers.get("location")
                if not location:
                    raise UnsafeTarget("redirect has no Location header")
                current = await self.validate_url(urljoin(current, location))
                continue
            if status != 200:
                raise httpx.HTTPStatusError(
                    f"website returned status {status}",
                    request=httpx.Request("GET", current),
                    response=httpx.Response(status),
                )
            content_type = headers.get("content-type", "").lower()
            if content_type and not (
                content_type.startswith("text/html") or content_type.startswith("text/plain")
            ):
                raise UnsafeTarget("only textual website content is accepted")
            return SafeResponse(final_url=current, content=content, content_type=content_type)
        raise UnsafeTarget("redirect resolution failed")


async def enrich_from_website(
    website: str | None, *, client: SecureWebClient | None = None
) -> dict:
    if not website:
        return {}
    secure_client = client or SecureWebClient()
    try:
        response = await secure_client.fetch_html(website)
        enrichment, relevant_urls = _page_enrichment(response)
        visited = {response.final_url.rstrip("/")}
        for page_url in relevant_urls[:4]:
            if page_url.rstrip("/") in visited:
                continue
            visited.add(page_url.rstrip("/"))
            try:
                contact_response = await secure_client.fetch_html(page_url)
                contact, _ = _page_enrichment(contact_response)
            except Exception:  # noqa: BLE001 - homepage evidence remains useful
                continue
            if contact:
                for field_name in ("name", "email", "phone"):
                    if not enrichment.get(field_name) and contact.get(field_name):
                        enrichment[field_name] = contact[field_name]
                if contact.get("locations"):
                    enrichment["locations"] = [
                        *(enrichment.get("locations") or []),
                        *contact["locations"],
                    ]
                enrichment["social_media"] = {
                    **(enrichment.get("social_media") or {}),
                    **(contact.get("social_media") or {}),
                } or None
                enrichment["specialties"] = sorted({*(enrichment.get("specialties") or []), *(contact.get("specialties") or [])}) or None
                enrichment["brands"] = sorted({*(enrichment.get("brands") or []), *(contact.get("brands") or [])}, key=str.casefold) or None
                enrichment["pages_visited"] = [*(enrichment.get("pages_visited") or []), contact_response.final_url]
    except Exception:  # noqa: BLE001 - enrichment is deliberately best-effort
        return {}
    return enrichment
