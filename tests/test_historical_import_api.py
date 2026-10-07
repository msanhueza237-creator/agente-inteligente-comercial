from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.web.import_auth import import_auth_client
from app.web.routes_import import router

USER_ID = str(UUID("11111111-1111-4111-8111-111111111111"))
CSV = b"Razon social;Email\nEmpresa de prueba;ventas@example.com\n"


@pytest.fixture
def endpoint():
    state = {"role": "vendedor", "active": True, "auth_status": 200,
             "profile_status": 200, "requests": []}
    settings = Settings(
        _env_file=None, database_url="postgresql+asyncpg://test:test@localhost/test",
        env="test", import_supabase_url="https://supabase.example.com",
        import_supabase_anon_key="synthetic-public-anon-key",
    )

    def upstream(request):
        state["requests"].append(request)
        assert request.method == "GET"  # No DB mutation or external import side effects.
        assert request.headers["authorization"] == "Bearer synthetic-session"
        assert request.headers["apikey"] == "synthetic-public-anon-key"
        if request.url.path == "/auth/v1/user":
            if state.get("unavailable"):
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(state["auth_status"], json={
                "id": USER_ID, "user_metadata": {"role": "administrador"},
            })
        assert request.url.path == "/rest/v1/profiles"
        assert request.url.params["id"] == f"eq.{USER_ID}"
        return httpx.Response(state["profile_status"], json=[{
            "role": state["role"], "active": state["active"],
        }])

    async def client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as value:
            yield value

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[import_auth_client] = client
    with TestClient(app) as browser:
        yield browser, state, settings


def post(browser, filename="clientes.csv", content=CSV, query=""):
    return browser.post(
        "/api/historical-imports/preview" + query,
        headers={"Authorization": "Bearer synthetic-session"},
        files={"file": (filename, content)},
    )


@pytest.mark.parametrize("role", ["administrador", "vendedor"])
def test_authenticated_preview_reuses_normalizer_without_database_writes(endpoint, role):
    browser, state, _ = endpoint
    state["role"] = role
    response = post(browser, query="?relationship_date=2026-10-07")
    assert response.status_code == 200
    body = response.json()
    assert body["rows"][0]["legal_name"] == "Empresa de prueba"
    assert body["relationship_date"] == "2026-10-07"
    assert body["stats"]["entities"] == 1
    assert len(state["requests"]) == 2


def test_missing_session_rejected_before_multipart_is_parsed(endpoint):
    browser, state, _ = endpoint
    response = browser.post("/api/historical-imports/preview", content=b"not multipart")
    assert response.status_code == 401
    assert state["requests"] == []


@pytest.mark.parametrize("role,active", [("visualizador", True), ("administrador", False)])
def test_profile_permissions_are_authoritative_not_user_metadata(endpoint, role, active):
    browser, state, _ = endpoint
    state.update(role=role, active=active)
    assert post(browser).status_code == 403


def test_expired_session(endpoint):
    browser, state, _ = endpoint
    state["auth_status"] = 401
    assert post(browser).status_code == 401
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("failure", ["unavailable", "auth_status", "profile_status"])
def test_upstream_failure_fails_closed(endpoint, failure):
    browser, state, _ = endpoint
    state[failure] = True if failure == "unavailable" else 500
    response = post(browser)
    assert response.status_code == 503
    assert "synthetic-session" not in response.text
    assert "supabase.example.com" not in response.text


def test_missing_configuration_disables_preview(endpoint):
    browser, state, settings = endpoint
    settings.import_supabase_url = ""
    assert post(browser).status_code == 503
    assert state["requests"] == []


def test_rejects_unsupported_file_and_invalid_date(endpoint):
    browser, _, _ = endpoint
    assert post(browser, filename="bad.exe").status_code == 415
    assert post(browser, query="?relationship_date=not-a-date").status_code == 422


def test_oversized_file_and_chunked_body_are_bounded(endpoint, monkeypatch):
    from app.web import routes_import
    monkeypatch.setattr(routes_import, "MAX_HISTORICAL_FILE_BYTES", 64)
    browser, _, _ = endpoint
    assert post(browser, content=b"x" * 65).status_code == 413
    response = browser.post(
        "/api/historical-imports/preview",
        headers={"Authorization": "Bearer synthetic-session",
                 "Content-Type": "multipart/form-data; boundary=test"},
        content=iter([b"--test\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.csv\"\r\n\r\n",
                      b"x" * 70000, b"\r\n--test--\r\n"]),
    )
    assert response.status_code == 413


@pytest.mark.parametrize("value", ["http://supabase.example.com", "https://user:key@example.com",
                                    "https://example.com/path", "https://*.example.com"])
def test_configuration_rejects_unsafe_auth_origins(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, database_url="postgresql+asyncpg://test:test@localhost/test",
                 import_supabase_url=value)


def test_actual_app_cors_allows_only_configured_crm(monkeypatch):
    import importlib

    from app import main
    monkeypatch.setenv("IMPORT_ALLOWED_ORIGINS", "https://crm.example.com")
    get_settings.cache_clear()
    try:
        importlib.reload(main)
        # No lifespan: never starts workers or connects to a database.
        browser = TestClient(main.app)
        headers = {"Origin": "https://crm.example.com", "Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "authorization,content-type"}
        response = browser.options("/api/historical-imports/preview", headers=headers)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://crm.example.com"
        headers["Origin"] = "https://untrusted.example.com"
        assert browser.options("/api/historical-imports/preview", headers=headers).status_code == 400
        browser.close()
    finally:
        monkeypatch.delenv("IMPORT_ALLOWED_ORIGINS")
        get_settings.cache_clear()
        importlib.reload(main)
