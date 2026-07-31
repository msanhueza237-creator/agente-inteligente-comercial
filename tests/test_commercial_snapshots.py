from datetime import date

from app.hub.commercial import build_commercial_report, extract_commercial_snapshot


def test_commercial_snapshot_builds_facto_customer_from_document_only() -> None:
    customers = extract_commercial_snapshot(
        [],
        [
            {
                "document_id": 501,
                "receiver_business_name": "Cliente desde factura SpA",
                "receiver_tax_id_code": "761112223",
                "receiver_email": "compras@clientefactura.cl",
                "issue_date": "2026-07-15",
                "net": 250000,
            }
        ],
        [],
        [],
        as_of=date(2026, 7, 30),
    )

    assert len(customers) == 1
    assert customers[0]["name"] == "Cliente desde factura SpA"
    assert customers[0]["tax_id"] == "761112223"
    assert customers[0]["facto_documents"] == 1
    assert customers[0]["facto_net_sales"] == 250000


def test_commercial_snapshot_uses_latest_facto_invoice_location_and_derives_region() -> None:
    customers = extract_commercial_snapshot(
        [],
        [
            {
                "document_id": 737,
                "issue_date": "2026-07-29",
                "net": 100000,
                "header": {
                    "receiver_business_name": (
                        "MARBA - REFRIGERACIÓN, AIRE ACONDICIONADO, CLIMATIZACION SPA"
                    ),
                    "receiver_tax_id_code": "76.919.986-1",
                    "receiver_address": "AVENIDA SAN MARTIN 01295",
                    "receiver_city": "TEMUCO",
                    "receiver_district": "TEMUCO",
                },
            },
            {
                "document_id": 719,
                "issue_date": "2026-07-08",
                "net": 50000,
                "header": {
                    "receiver_business_name": (
                        "MARBA - REFRIGERACIÓN, AIRE ACONDICIONADO, CLIMATIZACION SPA"
                    ),
                    "receiver_tax_id_code": "76.919.986-1",
                    "receiver_address": "DIRECCION HISTORICA",
                    "receiver_city": "TEMUCO",
                    "receiver_district": "AYSÉN",
                },
            },
        ],
        [],
        [],
        as_of=date(2026, 7, 30),
    )

    assert len(customers) == 1
    customer = customers[0]
    assert customer["address"] == "AVENIDA SAN MARTIN 01295"
    assert customer["city"] == "Temuco"
    assert customer["region"] == "Región de La Araucanía"
    assert customer["location_source"] == "facto_invoice"
    assert customer["location_verified_at"] == "2026-07-29"


def test_commercial_snapshot_prefers_comuna_over_generic_city() -> None:
    customers = extract_commercial_snapshot(
        [],
        [
            {
                "document_id": 1,
                "issue_date": "2026-07-20",
                "net": 1000,
                "header": {
                    "receiver_business_name": "Cliente Macul",
                    "receiver_tax_id_code": "76.000.001-1",
                    "receiver_address": "Los Plátanos 3406",
                    "receiver_city": "Santiago",
                    "receiver_district": "Macul",
                },
            }
        ],
        [],
        [],
        as_of=date(2026, 7, 30),
    )

    assert customers[0]["city"] == "Macul"
    assert customers[0]["region"] == "Región Metropolitana de Santiago"


def test_commercial_snapshot_unifies_exact_rut_without_duplicating_sales() -> None:
    customers = extract_commercial_snapshot(
        [
            {
                "id": 10,
                "business_name": "Clima Técnica SpA",
                "tax_id": "76.123.456-7",
                "email": "ventas@climatecnica.cl",
                "phone": "9 1111 2222",
            }
        ],
        [
            {
                "document_id": 99,
                "receiver_business_name": "Clima Técnica SpA",
                "receiver_tax_id_code": "761234567",
                "issue_date": "2026-07-10",
                "net": 100000,
                "tax": 19000,
                "gross": 119000,
                "details": [
                    {
                        "description": "Bomba de condensado Mini",
                        "quantity": 2,
                    }
                ],
            }
        ],
        [
            {
                "id": 20,
                "name": "Clima Tecnica",
                "identification": "76.123.456-7",
                "email": "compras@climatecnica.cl",
            }
        ],
        [
            {
                "id": 30,
                "created_at": "2026-07-20T12:00:00Z",
                "total": "119000.00",
                "customer": {
                    "id": 20,
                    "name": "Clima Tecnica",
                    "identification": "76.123.456-7",
                },
                "products": [
                    {
                        "name": "Bomba de condensado Mini",
                        "quantity": 1,
                    }
                ],
            }
        ],
        as_of=date(2026, 7, 30),
    )

    assert len(customers) == 1
    customer = customers[0]
    assert customer["source_channel"] == "both"
    assert customer["facto_net_sales"] == 100000
    assert customer["tiendanube_gross_sales"] == 119000
    assert customer["facto_documents"] == 1
    assert customer["tiendanube_orders"] == 1
    assert customer["lifecycle"] == "new"
    assert customer["whatsapp"] == "+56911112222"
    assert customer["purchase_months"] == {"2026-07": 2}
    assert customer["top_products"][0] == {
        "name": "Bomba de condensado Mini",
        "units": 3.0,
    }
    assert customer["product_families"][0] == {
        "name": "Bombas de condensado",
        "units": 3.0,
    }


def test_commercial_snapshot_does_not_merge_similar_names_without_exact_identity() -> None:
    customers = extract_commercial_snapshot(
        [{"id": 1, "business_name": "Frío Rojas", "tax_id": "11111111-1"}],
        [],
        [{"id": 2, "name": "Frio Rojas Chile", "email": "hola@otrodominio.cl"}],
        [],
        as_of=date(2026, 7, 30),
    )

    assert len(customers) == 2


def test_commercial_report_enriches_crm_type_and_prepares_reviewable_segments() -> None:
    snapshot = [
        {
            "customer_key": "rut:761234567",
            "name": "Servicio HVAC",
            "tax_id": "761234567",
            "email": "ventas@servicio.cl",
            "phone": "+56911112222",
            "whatsapp": "+56911112222",
            "sources": ["facto"],
            "source_channel": "facto_only",
            "facto_net_sales": 250000,
            "facto_documents": 3,
            "tiendanube_gross_sales": 0,
            "tiendanube_orders": 0,
            "first_purchase_at": "2025-01-10",
            "last_purchase_at": "2025-01-10",
            "lifecycle": "dormant",
            "days_since_purchase": 566,
            "contactable": True,
            "region": "",
            "city": "",
            "source_ids": {},
        }
    ]
    report = build_commercial_report(
        snapshot,
        [
            {
                "id": "crm-1",
                "name": "Servicio HVAC",
                "rut": "76.123.456-7",
                "type": "tecnico",
                "status": "cliente",
                "priority": "alta",
                "region": "Metropolitana de Santiago",
                "city": "Macul",
            }
        ],
    )

    assert report["metrics"]["customers"] == 1
    assert report["customers"][0]["crm_type"] == "tecnico"
    assert report["customers"][0]["city"] == "Macul"
    segment_ids = {segment["id"] for segment in report["segments"]}
    assert "dormant_customers" in segment_ids
    assert "hvac_technicians" in segment_ids
    assert all(segment["company_ids"] == ["crm-1"] for segment in report["segments"])


def test_commercial_report_prioritizes_web_and_high_value_recovery() -> None:
    report = build_commercial_report(
        [
            {
                "customer_key": "rut:760000001",
                "name": "Cliente Facto Valioso",
                "tax_id": "760000001",
                "email": "compras@valioso.cl",
                "sources": ["facto"],
                "source_channel": "facto_only",
                "facto_net_sales": 8_000_000,
                "facto_documents": 8,
                "tiendanube_gross_sales": 0,
                "tiendanube_orders": 0,
                "first_purchase_at": "2025-01-01",
                "last_purchase_at": "2026-01-01",
                "purchase_months": {"2025-01": 1, "2026-01": 1},
                "lifecycle": "dormant",
                "contactable": True,
                "region": "Metropolitana",
                "city": "Santiago",
                "source_ids": {},
            },
            {
                "customer_key": "email:web@cliente.cl",
                "name": "Comprador Web",
                "email": "web@cliente.cl",
                "phone": "+56911112222",
                "whatsapp": "+56911112222",
                "sources": ["tiendanube"],
                "source_channel": "tiendanube_only",
                "facto_net_sales": 0,
                "facto_documents": 0,
                "tiendanube_gross_sales": 238_000,
                "tiendanube_orders": 2,
                "first_purchase_at": "2026-06-01",
                "last_purchase_at": "2026-07-01",
                "purchase_months": {"2026-06": 1, "2026-07": 1},
                "lifecycle": "active",
                "contactable": True,
                "region": "Metropolitana",
                "city": "Ñuñoa",
                "source_ids": {},
            },
        ],
        [],
    )

    customers = {row["name"]: row for row in report["customers"]}
    valuable = customers["Cliente Facto Valioso"]
    web = customers["Comprador Web"]
    assert valuable["recommended_action"] == "rescue_priority"
    assert valuable["opportunity_priority"] == "urgent"
    assert valuable["value_tier"] in {"A", "B"}
    assert web["recommended_action"] == "convert_web_to_b2b"
    assert web["whatsapp_ready"] is True
    assert report["metrics"]["customers_at_risk"] == 1
    assert report["metrics"]["campaign_ready"] == 2
    assert report["opportunity_counts"]["urgent"] == 1
    segment_ids = {segment["id"] for segment in report["segments"]}
    assert "valuable_customers_to_rescue" in segment_ids
    assert "web_customers_to_develop" in segment_ids
    assert report["acquisition_by_month"][-1]["returning_customers"] == 1


def test_commercial_report_owns_channel_rankings_and_sold_products() -> None:
    report = build_commercial_report(
        [
            {
                "customer_key": "rut:760000001",
                "name": "Cliente Facto",
                "tax_id": "760000001",
                "email": "facto@cliente.cl",
                "sources": ["facto"],
                "source_channel": "facto_only",
                "facto_net_sales": 900_000,
                "facto_documents": 4,
                "tiendanube_gross_sales": 0,
                "tiendanube_orders": 0,
                "first_purchase_at": "2025-01-01",
                "last_purchase_at": "2026-07-01",
                "lifecycle": "active",
                "contactable": True,
                "source_ids": {},
            },
            {
                "customer_key": "email:web@cliente.cl",
                "name": "Cliente Web",
                "email": "web@cliente.cl",
                "sources": ["tiendanube"],
                "source_channel": "tiendanube_only",
                "facto_net_sales": 0,
                "facto_documents": 0,
                "tiendanube_gross_sales": 238_000,
                "tiendanube_orders": 2,
                "first_purchase_at": "2026-06-01",
                "last_purchase_at": "2026-07-01",
                "lifecycle": "active",
                "contactable": True,
                "source_ids": {},
            },
        ],
        [],
        {
            "top_products": [
                {
                    "name": "Bomba de condensado",
                    "sku": "BC-01",
                    "units": 12,
                    "net_sales_observed": 480_000,
                }
            ]
        },
    )

    assert report["facto_ranking"][0]["name"] == "Cliente Facto"
    assert report["facto_ranking"][0]["net_sales"] == 900_000
    assert report["tiendanube_ranking"][0]["name"] == "Cliente Web"
    assert report["tiendanube_ranking"][0]["net_sales"] == 200_000
    assert report["sales_products"][0] == {
        "name": "Bomba de condensado",
        "sku": "BC-01",
        "units": 12.0,
        "net_sales": 480_000.0,
    }
