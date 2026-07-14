import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.crm.fake import FakeCRMPort
from app.crm.http import HttpCRMPort
from app.scheduler.worker import build_crm_port


def settings(**overrides):
    values = {
        "database_url": "postgresql+asyncpg://postgres:postgres@localhost/test",
        "env": "test",
        **overrides,
    }
    return Settings(**values)


def test_fake_port_remains_available_only_outside_production():
    assert isinstance(build_crm_port(settings()), FakeCRMPort)
    with pytest.raises(ValidationError, match="CRM_MODE=http"):
        settings(env="production", crm_mode="fake")


def test_http_port_requires_https_and_secret():
    with pytest.raises(ValidationError, match="HTTPS"):
        settings(crm_mode="http", crm_base_url="http://crm.test", crm_api_key="secret")
    with pytest.raises(ValidationError, match="CRM_API_KEY"):
        settings(crm_mode="http", crm_base_url="https://crm.test")


def test_production_builds_restricted_http_port():
    configured = settings(
        env="production",
        crm_mode="http",
        crm_base_url="https://supabase.example/functions/v1/crm-agent",
        crm_api_key=SecretStr("ca_live_test"),
    )
    port = build_crm_port(configured)
    assert isinstance(port, HttpCRMPort)
    assert port.base_url == "https://supabase.example/functions/v1/crm-agent/"
