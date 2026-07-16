from types import SimpleNamespace

import pytest

from app.integrations.monitor import IntegrationMonitor


class RecordingCRM:
    def __init__(self):
        self.reported = []

    async def report_integration_status(self, **payload):
        self.reported.append(payload)


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeClient:
    def __init__(self, *, status_code: int, **_kwargs):
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args, **_kwargs):
        return FakeResponse(self.status_code)


@pytest.mark.asyncio
async def test_brave_connection_check_reports_connected_without_exposing_key(monkeypatch):
    crm = RecordingCRM()
    settings = SimpleNamespace(crm_worker_id="worker-test", brave_search_api_key="secret")
    monkeypatch.setattr(
        "app.integrations.monitor.httpx.AsyncClient",
        lambda **kwargs: FakeClient(status_code=200, **kwargs),
    )

    await IntegrationMonitor(crm, settings)._check_brave_search("check-1")

    assert crm.reported == [
        {
            "worker_id": "worker-test",
            "check_id": "check-1",
            "provider": "brave_search",
            "configured": True,
            "status": "connected",
            "error_code": None,
                "message": "Brave Search respondio correctamente.",
                "metadata": {
                    "cost_per_query_usd": 0.005,
                    "monthly_limit_usd": 5.0,
                    "free_credit_usd": 5.0,
                },
            }
    ]


@pytest.mark.asyncio
async def test_brave_connection_check_reports_invalid_credential(monkeypatch):
    crm = RecordingCRM()
    settings = SimpleNamespace(crm_worker_id="worker-test", brave_search_api_key="secret")
    monkeypatch.setattr(
        "app.integrations.monitor.httpx.AsyncClient",
        lambda **kwargs: FakeClient(status_code=401, **kwargs),
    )

    await IntegrationMonitor(crm, settings)._check_brave_search("check-1")

    assert crm.reported[0]["status"] == "error"
    assert crm.reported[0]["error_code"] == "forbidden"
