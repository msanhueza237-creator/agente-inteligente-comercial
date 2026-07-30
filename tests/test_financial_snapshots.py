from datetime import date

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
                "document_type_id": 32,
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
            "document_type_id": 32,
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


def test_financial_snapshot_compares_ytd_with_same_prior_year_period() -> None:
    documents = [
        {
            "document_type_id": 32,
            "issue_date": "2025-01-10",
            "total_amount": 100000,
        },
        {
            "document_type_id": 32,
            "issue_date": "2025-07-15",
            "total_amount": 200000,
        },
        {
            "document_type_id": 32,
            "issue_date": "2025-12-10",
            "total_amount": 400000,
        },
        {
            "document_type_id": 32,
            "issue_date": "2026-01-12",
            "total_amount": 150000,
        },
        {
            "document_type_id": 32,
            "issue_date": "2026-07-15",
            "total_amount": 300000,
        },
    ]

    comparison = extract_financial_snapshot(
        documents,
        generated_on=date(2026, 7, 15),
    )[0]["payload"]["year_comparison"]

    assert comparison["current_year"] == 2026
    assert comparison["previous_year"] == 2025
    assert comparison["cutoff_date"] == "2026-07-15"
    assert comparison["previous_cutoff_date"] == "2025-07-15"
    assert comparison["current_ytd_net_sales"] == 450000
    assert comparison["previous_ytd_net_sales"] == 300000
    assert comparison["previous_full_year_net_sales"] == 700000
    assert comparison["growth_amount"] == 150000
    assert comparison["growth_percent"] == 50
    assert comparison["current_ytd_documents"] == 2
    assert comparison["previous_ytd_documents"] == 2
    assert comparison["months"][0] == {
        "month": 1,
        "label": "Ene",
        "current_net_sales": 150000,
        "previous_net_sales": 100000,
        "current_net_purchases": 0,
        "previous_net_purchases": 0,
    }


def test_financial_snapshot_uses_latest_synchronized_document_as_cutoff() -> None:
    report = extract_financial_snapshot(
        [
            {
                "document_type_id": 32,
                "issue_date": "2025-07-29",
                "total_amount": 100000,
            },
            {
                "document_type_id": 32,
                "issue_date": "2026-07-29",
                "total_amount": 200000,
            },
        ]
    )[0]["payload"]

    assert report["period_end"] == "2026-07-29"
    assert report["year_comparison"]["cutoff_date"] == "2026-07-29"
    assert report["year_comparison"]["previous_cutoff_date"] == "2025-07-29"


def test_financial_snapshot_adds_net_purchases_and_supplier_ranking() -> None:
    purchases = [
        {
            "document_id": 101,
            "document_type_id": 9,
            "issue_date": "2025-03-10",
            "net_amount": 100000,
            "tax_amount": 19000,
            "total_amount": 119000,
            "issuer_legal_name": "Proveedor China Uno",
            "issuer_tax_id_code": "EXT-001",
        },
        {
            "document_id": 102,
            "document_type_id": 9,
            "issue_date": "2026-03-10",
            "net_amount": 250000,
            "tax_amount": 47500,
            "total_amount": 297500,
            "issuer_legal_name": "Proveedor China Uno",
            "issuer_tax_id_code": "EXT-001",
        },
        {
            "document_id": 103,
            "document_type_id": 28,
            "issue_date": "2026-03-20",
            "net_amount": 50000,
            "tax_amount": 9500,
            "total_amount": 59500,
            "issuer_legal_name": "Proveedor China Uno",
            "issuer_tax_id_code": "EXT-001",
        },
        {
            "document_id": 104,
            "document_type_id": 33,
            "issue_date": "2026-05-02",
            "total_amount": 80000,
            "issuer_legal_name": "Proveedor Local Dos",
            "issuer_tax_id_code": "76.111.222-3",
        },
    ]

    report = extract_financial_snapshot(
        [],
        generated_on=date(2026, 7, 29),
        purchase_documents_payload=purchases,
    )[0]["payload"]

    assert report["net_purchases"] == 380000
    assert report["purchase_document_count"] == 4
    assert report["supplier_count"] == 2
    assert report["purchases_available"] is True
    assert report["top_suppliers"][0]["name"] == "Proveedor China Uno"
    assert report["top_suppliers"][0]["net_purchases"] == 300000
    assert report["top_suppliers"][0]["years"]["2026"]["net_purchases"] == 200000
    comparison = report["year_comparison"]
    assert comparison["current_ytd_net_purchases"] == 280000
    assert comparison["previous_ytd_net_purchases"] == 100000
    assert comparison["purchase_growth_percent"] == 180
    assert comparison["months"][2]["current_net_purchases"] == 200000
    assert comparison["months"][2]["previous_net_purchases"] == 100000


def test_financial_snapshot_builds_exact_receivables_from_registered_payments() -> None:
    documents = [
        {
            "document_id": "INV-1",
            "document_number": "100",
            "document_type_id": 2,
            "header": {
                "issue_date": "2026-06-01",
                "payment_conditions": "30",
                "total_amount": 119000,
                "receiver_legal_name": "Cliente Deudor SpA",
                "receiver_tax_id_code": "76.500.000-1",
            },
        },
        {
            "document_id": "INV-2",
            "document_number": "101",
            "document_type_id": 2,
            "header": {
                "issue_date": "2026-07-20",
                "payment_conditions": "30",
                "total_amount": 238000,
                "receiver_legal_name": "Cliente Vigente SpA",
                "receiver_tax_id_code": "76.500.000-2",
            },
        },
    ]
    payments = [
        {
            "payment_id": "P-1",
            "document_id": "INV-1",
            "payment_date": "2026-06-20",
            "payment_amount": 19000,
        }
    ]

    report = extract_financial_snapshot(
        documents,
        generated_on=date(2026, 7, 29),
        payments_payload=payments,
    )[0]["payload"]
    collections = report["collections"]

    assert report["receivables_available"] is True
    assert collections["mode"] == "registered_payments"
    assert collections["observed_amount"] == 338000
    assert collections["overdue_amount"] == 100000
    assert collections["due_next_30"] == 238000
    assert collections["payment_count"] == 1
    assert collections["payments_registered"] == 19000
    assert collections["customers"][0]["name"] == "Cliente Vigente SpA"


def test_financial_snapshot_labels_credit_exposure_when_payment_list_is_missing() -> None:
    documents = [
        {
            "document_id": "CREDIT-1",
            "document_type_id": 2,
            "header": {
                "issue_date": "2026-07-01",
                "payment_conditions": "0,30,60",
                "total_amount": 119000,
                "receiver_legal_name": "Cliente Credito",
            },
        },
        {
            "document_id": "CASH-1",
            "document_type_id": 2,
            "header": {
                "issue_date": "2026-07-01",
                "payment_conditions": "0",
                "total_amount": 59500,
                "receiver_legal_name": "Cliente Contado",
            },
        },
    ]

    report = extract_financial_snapshot(
        documents,
        generated_on=date(2026, 7, 29),
    )[0]["payload"]
    collections = report["collections"]

    assert report["receivables_available"] is False
    assert report["credit_exposure_available"] is True
    assert collections["mode"] == "documentary_credit"
    assert collections["observed_amount"] == 119000
    assert collections["documents"] == 1
    assert collections["reviewed_documents"] == 2
    assert collections["credit_documents"] == 1
    assert collections["cash_documents"] == 1
    assert collections["cash_amount"] == 59500
    assert collections["unclassified_documents"] == 0
    assert collections["classification_status"] == "complete"
    assert "No confirma" in collections["disclaimer"]
    assert report["documentary_cash_flow"]["cash_balance_available"] is False


def test_financial_snapshot_reports_invoices_without_payment_classification() -> None:
    documents = [
        {
            "document_id": "UNKNOWN-1",
            "document_type_id": 2,
            "header": {
                "issue_date": "2026-07-01",
                "total_amount": 119000,
                "receiver_legal_name": "Cliente sin condicion",
            },
        },
        {
            "document_id": "CASH-1",
            "document_type_id": 2,
            "header": {
                "issue_date": "2026-07-02",
                "payment_conditions": "Contado",
                "total_amount": 59500,
                "receiver_legal_name": "Cliente contado",
            },
        },
    ]

    report = extract_financial_snapshot(
        documents,
        generated_on=date(2026, 7, 29),
    )[0]["payload"]
    collections = report["collections"]

    assert collections["observed_amount"] == 0
    assert collections["reviewed_documents"] == 2
    assert collections["reviewed_amount"] == 178500
    assert collections["cash_documents"] == 1
    assert collections["cash_amount"] == 59500
    assert collections["unclassified_documents"] == 1
    assert collections["unclassified_amount"] == 119000
    assert collections["classification_status"] == "partial"


def test_financial_snapshot_recognizes_nested_due_date_as_credit() -> None:
    documents = [
        {
            "document_id": "NESTED-1",
            "document_type_id": 2,
            "header": {
                "issue_date": "2026-07-01",
                "total_amount": 119000,
                "receiver_legal_name": "Cliente con vencimiento",
            },
            "payment_information": {
                "due_date": "2026-08-15",
            },
        }
    ]

    report = extract_financial_snapshot(
        documents,
        generated_on=date(2026, 7, 29),
    )[0]["payload"]
    collections = report["collections"]

    assert collections["observed_amount"] == 119000
    assert collections["credit_documents"] == 1
    assert collections["due_next_30"] == 119000
