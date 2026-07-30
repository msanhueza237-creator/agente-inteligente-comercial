from app.hub.finance import extract_financial_snapshot


def test_financial_snapshot_separates_net_sales_and_vat() -> None:
    rows = extract_financial_snapshot(
        [
            {
                "document_id": 1,
                "document_type_id": 2,
                "header": {
                    "issue_date": "2026-06-10",
                    "net_amount": 100000,
                    "tax_amount": 19000,
                    "total_amount": 119000,
                    "receiver": {"business_name": "Cliente Uno", "rut": "76.000.000-1"},
                },
            },
            {
                "document_id": 2,
                "document_type_id": 28,
                "header": {
                    "issue_date": "2026-07-05",
                    "total_amount": 50000,
                    "receiver": {"business_name": "Cliente Dos"},
                },
            },
        ],
        [
            {
                "payload": {
                    "sku": "SKU-1",
                    "name": "Producto",
                    "units_sold_observed": 2,
                    "sales_revenue_observed": 100000,
                    "unit_cost_source": 30000,
                    "cost_available_in_source": True,
                }
            }
        ],
    )

    report = rows[0]["payload"]
    assert report["net_sales"] == 150000
    assert report["tax"] == 19000
    assert report["gross_sales"] == 169000
    assert report["document_count"] == 2
    assert report["average_net_ticket"] == 75000
    assert report["reference_cost_of_sales"] == 60000
    assert report["reference_gross_margin"] == 90000
    assert [month["month"] for month in report["sales_by_month"]] == ["2026-06", "2026-07"]
    assert report["top_customers"][0]["name"] == "Cliente Uno"


def test_financial_snapshot_derives_net_from_taxed_gross_only() -> None:
    rows = extract_financial_snapshot(
        [{"document_type_id": 37, "issue_date": "2026-07-05", "total_amount": 119000}]
    )

    report = rows[0]["payload"]
    assert round(report["net_sales"]) == 100000
    assert round(report["tax"]) == 19000
    assert report["receivables_available"] is False


def test_financial_snapshot_reads_flat_facto_receiver_fields() -> None:
    rows = extract_financial_snapshot(
        [
            {
                "document_id": 3,
                "document_type_id": 2,
                "header": {
                    "issue_date": "2026-07-15",
                    "net_amount": 250000,
                    "tax_amount": 47500,
                    "total_amount": 297500,
                    "receiver_legal_name": "Clima Cliente SpA",
                    "receiver_tax_id_code": "76.123.456-7",
                },
            }
        ]
    )

    report = rows[0]["payload"]
    assert report["customer_count"] == 1
    assert report["top_customers"] == [
        {
            "name": "Clima Cliente SpA",
            "tax_id": "76.123.456-7",
            "net_sales": 250000.0,
            "documents": 1,
        }
    ]


def test_financial_snapshot_keeps_full_customer_ranking() -> None:
    documents = [
        {
            "document_id": customer,
            "document_type_id": 28,
            "issue_date": "2026-07-20",
            "total_amount": customer * 1000,
            "receiver_legal_name": f"Cliente {customer}",
            "receiver_tax_id_code": f"76.000.{customer:03d}-K",
        }
        for customer in range(1, 36)
    ]

    report = extract_financial_snapshot(documents)[0]["payload"]
    assert report["customer_count"] == 35
    assert len(report["top_customers"]) == 35
    assert report["top_customers"][0]["name"] == "Cliente 35"
