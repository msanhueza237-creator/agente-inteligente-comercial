import asyncio
from collections import Counter

from app.hub.worker import load_facto_details
from app.integrations.facto import FactoClient, FactoError
from app.config import Settings
import httpx
import pytest


async def test_full_catalog_retries_transient_failures_without_a_50_product_cutoff(monkeypatch):
    calls = Counter()
    active, peak = 0, 0
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr('app.hub.worker.asyncio.sleep', fast_sleep)

    class Client:
        async def product(self, product_id):
            nonlocal active, peak
            calls[product_id] += 1
            active += 1
            peak = max(active, peak)
            await real_sleep(0)
            active -= 1
            if product_id > 50 and calls[product_id] == 1:
                raise RuntimeError('Temporary failure')
            return {'data': {'product': {'product_id': product_id, 'sku': f'P-{product_id}', 'inventories': {'details': [{'product_location_id': '1', 'available_quantity': '5'}]}}}}

    rows = [{'product_id': i, 'sku': f'P-{i}'} for i in range(1, 253)]
    products, documents = await load_facto_details(Client(), rows, [])
    assert len(products) == 252
    assert all(p['inventories']['details'][0]['available_quantity'] == '5' for p in products)
    assert calls[252] == 2
    assert peak <= 2
    assert documents == []


async def test_permanent_failure_or_wrong_identity_never_copies_foreign_stock(monkeypatch):
    calls = Counter()

    async def fast_sleep(_seconds):
        pass

    monkeypatch.setattr('app.hub.worker.asyncio.sleep', fast_sleep)

    class Client:
        async def product(self, product_id):
            calls[product_id] += 1
            if product_id == 1:
                raise RuntimeError('Unavailable')
            return {'product_id': 450, 'sku': 'WRONG', 'inventories': {'details': []}}

    rows = [{'product_id': 1, 'sku': 'P-1'}, {'product_id': 2, 'sku': 'P-2'}]
    products, _ = await load_facto_details(Client(), rows, [])
    assert products == rows
    assert calls == {1: 4, 2: 4}


async def test_rate_limit_preserves_retry_after_and_waits_before_retry(monkeypatch):
    async def handler(request):
        return httpx.Response(429, headers={'Retry-After': '45'}, text='private body not logged')

    client = FactoClient(Settings(), transport=httpx.MockTransport(handler))
    client._access_token = 'fixture'
    # Avoid authentication: this test exercises only the read endpoint error contract.
    async def token():
        return 'fixture'
    monkeypatch.setattr(client, '_authenticate', token)
    with pytest.raises(FactoError) as caught:
        await client.product(1)
    assert caught.value.status_code == 429
    assert caught.value.retry_after == 45
    assert 'private' not in str(caught.value)

    delays = []
    async def sleep(seconds):
        delays.append(seconds)
    monkeypatch.setattr('app.hub.worker.asyncio.sleep', sleep)
    calls = 0
    class LimitedClient:
        async def product(self, product_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise caught.value
            return {'product_id': product_id, 'inventories': {'details': []}}
    result, _ = await load_facto_details(LimitedClient(), [{'product_id': 1}], [])
    assert calls == 2
    assert any(44 < seconds <= 45 for seconds in delays)
    assert 'inventories' in result[0]
