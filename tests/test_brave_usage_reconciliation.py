from app.prospecting.budget import brave_provider_usage_from_headers


def test_brave_monthly_usage_uses_last_rate_limit_window():
    usage = brave_provider_usage_from_headers(
        {
            "X-RateLimit-Limit": "1, 1000",
            "X-RateLimit-Remaining": "0, 450",
            "X-RateLimit-Reset": "1, 1296000",
        }
    )

    assert usage == (550, 1000, 1296000)


def test_brave_usage_ignores_missing_monthly_quota():
    assert brave_provider_usage_from_headers({}) is None

