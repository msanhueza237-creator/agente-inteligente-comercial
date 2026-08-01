from app.hub.marketing import build_marketing_report


def test_marketing_report_uses_real_audiences_and_only_eligible_stock():
    commercial_report = {
        "metrics": {
            "customers": 2,
            "contactable": 2,
            "email_ready": 1,
            "whatsapp_ready": 2,
        },
        "customers": [
            {"customer_key": "rut:111", "name": "Tecnico Uno"},
            {"customer_key": "rut:222", "name": "Tecnico Dos"},
        ],
        "segments": [
            {
                "id": "hvac_technicians",
                "name": "Tecnicos e instaladores",
                "reason": "Cartera HVAC revisada",
                "channel": "whatsapp",
                "count": 2,
                "email_count": 1,
                "whatsapp_count": 2,
                "customer_keys": ["rut:111", "rut:222"],
                "company_ids": ["company-1"],
                "priority": "high",
            }
        ],
    }
    inventory = [
        {
            "sku": "STOCK-OK",
            "name": "Bomba de condensado",
            "stock_known": True,
            "available_units": 100,
            "average_daily_demand": 1,
            "units_sold_observed": 25,
            "sales_revenue_observed": 500_000,
            "unit_price": 20_000,
            "unit_price_is_net": True,
        },
        {
            "sku": "SIN-STOCK",
            "name": "Producto agotado",
            "stock_known": True,
            "available_units": 0,
            "average_daily_demand": 1,
        },
        {
            "sku": "COBERTURA-CORTA",
            "name": "Producto reservado",
            "stock_known": True,
            "available_units": 10,
            "average_daily_demand": 1,
        },
    ]

    report = build_marketing_report(
        commercial_report,
        inventory,
        {"automatic_sending": False, "approved_benefits": {}},
        as_of="2026-08-01",
    )

    assert report["strategy"]["season"] == "preseason"
    assert report["strategy"]["automatic_sending"] is False
    assert report["strategy"]["human_approval_required"] is True
    assert report["metrics"]["campaign_briefs"] == 1
    assert report["metrics"]["products_eligible"] == 1
    assert report["metrics"]["excluded_no_stock"] == 1
    assert report["metrics"]["excluded_low_coverage"] == 1

    brief = report["campaign_briefs"][0]
    assert brief["status"] == "draft"
    assert brief["requires_approval"] is True
    assert brief["benefit"] == ""
    assert brief["audience"]["customer_keys"] == ["rut:111", "rut:222"]
    assert brief["product"]["sku"] == "STOCK-OK"
    assert "Producto agotado" not in brief["email_body"]


def test_marketing_report_only_uses_explicitly_approved_benefits():
    commercial_report = {
        "metrics": {"customers": 1, "contactable": 1},
        "customers": [{"customer_key": "rut:333", "name": "Instalador"}],
        "segments": [
            {
                "id": "hvac_technicians",
                "name": "Tecnicos",
                "channel": "email",
                "count": 1,
                "customer_keys": ["rut:333"],
            }
        ],
    }
    inventory = [{"sku": "SKU-1", "name": "Herramienta HVAC", "stock_known": True, "available_units": 20}]
    benefit = "Beneficio aprobado por administracion"

    report = build_marketing_report(
        commercial_report,
        inventory,
        {"approved_benefits": {"hvac_technicians": benefit}},
        as_of="2026-03-01",
    )

    brief = report["campaign_briefs"][0]
    assert brief["benefit"] == benefit
    assert benefit in brief["email_body"]
