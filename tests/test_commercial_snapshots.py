from datetime import date

from app.hub.commercial import build_commercial_report, extract_commercial_snapshot


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
