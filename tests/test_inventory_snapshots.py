from app.hub.inventory import eligible_replenishment_payloads, extract_product_snapshots
from app.hub.worker import (
    load_facto_details,
    load_facto_sales_documents,
    load_paginated_records,
)


def test_inventory_snapshot_requires_explicit_stock_cost_and_sales_evidence() -> None:
    snapshots = extract_product_snapshots(
        {"data": [{"sku": "AC-01", "name": "Equipo", "stock": 12, "cost_usd": 100}]},
        {
            "data": [
                {"issue_date": "2026-07-01", "items": [{"sku": "AC-01", "quantity": 2}]},
                {"issue_date": "2026-07-10", "items": [{"sku": "AC-01", "quantity": 8}]},
            ]
        },
    )

    snapshot = snapshots[0]["payload"]
    assert snapshot["available_units"] == 12.0
    assert snapshot["average_daily_demand"] == 1.0
    assert snapshot["demand_available"] is True

    eligible = eligible_replenishment_payloads(snapshots, "2026-07-10")
    assert eligible[0]["sku"] == "AC-01"
    assert eligible[0]["unit_cost_usd"] == 100.0


def test_inventory_snapshot_does_not_invent_demand_without_documents() -> None:
    snapshots = extract_product_snapshots(
        {"data": [{"sku": "AC-02", "stock": 4, "cost_usd": 20}]},
        {"data": []},
    )

    assert snapshots[0]["payload"]["demand_available"] is False
    assert eligible_replenishment_payloads(snapshots, "2026-07-10") == []


def test_inventory_snapshot_accepts_nested_spanish_facto_fields() -> None:
    snapshots = extract_product_snapshots(
        {
            "data": {
                "products": [
                    {
                        "codigoProducto": "ST-4BMC",
                        "nombre": "Bomba de vacio",
                        "stockActual": 6,
                        "costoNeto": 85,
                        "precioNeto": 149,
                    }
                ]
            }
        },
        {
            "result": {
                "documents": [
                    {
                        "fechaEmision": "2026-07-01",
                        "detalles": [{"codigoProducto": "ST-4BMC", "cantidad": 3}],
                    },
                    {
                        "fechaEmision": "2026-07-10",
                        "detalles": [{"codigoProducto": "ST-4BMC", "cantidad": 7}],
                    },
                ]
            }
        },
    )

    snapshot = snapshots[0]["payload"]
    assert snapshot["sku"] == "ST-4BMC"
    assert snapshot["available_units"] == 6.0
    assert snapshot["unit_cost_source"] == 85.0
    assert snapshot["cost_requires_usd_conversion"] is True
    assert snapshot["average_daily_demand"] == 1.0


def test_inventory_snapshot_prefers_facto_warehouse_stock_over_generic_quantity() -> None:
    snapshots = extract_product_snapshots(
        {
            "data": [
                {
                    "product_id": 10,
                    "sku": "BOD-10",
                    "name": "Producto en bodega",
                    "quantity": 0,
                    "inventories": {
                        "total_available": 17,
                        "total_reserved": 2,
                        "details": [
                            {
                                "product_location_id": 4,
                                "available_quantity": 17,
                                "reserved_quantity": 2,
                            }
                        ],
                    },
                }
            ]
        },
        {},
    )

    payload = snapshots[0]["payload"]
    assert payload["stock_known"] is True
    assert payload["available_units"] == 17
    assert payload["warehouse_stock"] == [
        {
            "product_location_id": 4,
            "available_quantity": 17,
            "reserved_quantity": 2,
        }
    ]


def test_inventory_snapshot_does_not_treat_invoice_quantity_as_stock() -> None:
    snapshots = extract_product_snapshots(
        {"data": [{"product_id": 11, "sku": "SIN-BODEGA", "quantity": 0}]},
        {},
    )

    payload = snapshots[0]["payload"]
    assert payload["stock_known"] is False
    assert payload["available_units"] == 0


def test_inventory_snapshot_sums_facto_warehouse_details_when_total_is_missing() -> None:
    snapshots = extract_product_snapshots(
        {
            "data": [
                {
                    "product_id": 12,
                    "sku": "MULTI-BODEGA",
                    "inventories": {
                        "details": [
                            {"product_location_id": 1, "available_quantity": 3},
                            {"product_location_id": 2, "available_quantity": 5},
                        ]
                    },
                }
            ]
        },
        {},
    )

    assert snapshots[0]["payload"]["available_units"] == 8
    assert snapshots[0]["payload"]["stock_known"] is True


def test_inventory_snapshot_uses_warehouse_details_when_facto_total_is_stale() -> None:
    snapshots = extract_product_snapshots(
        {
            "data": [
                {
                    "product_id": 236,
                    "sku": "RBA-450",
                    "inventories": {
                        "total_available": 0,
                        "details": [
                            {"product_location_id": 2, "available_quantity": "0.000000"},
                            {"product_location_id": 1, "available_quantity": "96.000000"},
                        ],
                    },
                }
            ]
        },
        {},
    )

    payload = snapshots[0]["payload"]
    assert payload["stock_known"] is True
    assert payload["available_units"] == 96


def test_inventory_snapshot_finds_provider_specific_envelope() -> None:
    snapshots = extract_product_snapshots(
        {
            "respuestaFacto": {
                "catalogo": [
                    {"codigo": "P-1", "existencia": 9, "costo": 4, "precio": 8}
                ]
            }
        },
        {"respuestaFacto": {"ventas": []}},
    )

    assert snapshots[0]["payload"]["sku"] == "P-1"
    assert snapshots[0]["payload"]["available_units"] == 9.0
    assert round(snapshots[0]["payload"]["unit_price"], 2) == 6.72
    assert snapshots[0]["payload"]["source_price_includes_tax"] is True


def test_inventory_snapshot_reads_facto_product_and_document_details() -> None:
    snapshots = extract_product_snapshots(
        [
            {
                "product_id": 40,
                "sku": "KT-6018",
                "name": "Control remoto",
                "cost": {"currency_id": 39, "value": 5000},
                "prices": [{"unit_net": 8000, "currency_id": 39}],
                "inventories": {
                    "total_available": 15,
                    "total_reserved": 2,
                    "details": [{"product_location_id": 1, "available_quantity": 15}],
                },
            }
        ],
        [
            {
                "document_id": 100,
                "header": {"issue_date": "2026-07-01"},
                "details": [{"sku": "KT-6018", "quantity": 2}],
            },
            {
                "document_id": 101,
                "header": {"issue_date": "2026-07-10"},
                "details": [{"sku": "KT-6018", "quantity": 8}],
            },
        ],
    )

    snapshot = snapshots[0]["payload"]
    assert snapshot["available_units"] == 15.0
    assert snapshot["stock_known"] is True
    assert snapshot["unit_cost_source"] == 5000.0
    assert snapshot["unit_cost_usd"] == 0.0
    assert snapshot["cost_known"] is False
    assert snapshot["cost_requires_usd_conversion"] is True
    assert snapshot["unit_price"] == 8000.0
    assert snapshot["unit_price_is_net"] is True
    assert snapshot["source_price_includes_tax"] is False
    assert snapshot["average_daily_demand"] == 1.0
    assert snapshot["demand_available"] is True


def test_inventory_snapshot_matches_facto_sale_line_by_exact_product_name() -> None:
    snapshots = extract_product_snapshots(
        [
            {
                "product_id": 40,
                "sku": "KT-6018",
                "name": "Control remoto universal",
                "cost": {"value": 5000},
                "inventories": {"details": [{"available_quantity": 15}]},
            },
            {
                "product_id": 41,
                "sku": "OTRO",
                "name": "Otro producto",
                "inventories": {"details": [{"available_quantity": 3}]},
            },
        ],
        [
            {
                "document_id": 100,
                "header": {"issue_date": "2026-07-01"},
                "details": [
                    {
                        "line_description": "CONTROL REMOTO UNIVERSAL",
                        "quantity": 2,
                        "unit_price": 8000,
                    },
                    {
                        "line_description": "Gastos de envío",
                        "quantity": 1,
                        "unit_price": 5000,
                    },
                ],
            },
            {
                "document_id": 101,
                "header": {"issue_date": "2026-07-10"},
                "details": [
                    {
                        "line_description": "Control remoto universal",
                        "quantity": 8,
                        "unit_price": 8000,
                    }
                ],
            },
        ],
    )

    remote = snapshots[0]["payload"]
    other = snapshots[1]["payload"]
    assert remote["units_sold_observed"] == 10
    assert remote["sales_revenue_observed"] == 80000
    assert remote["sales_document_count"] == 2
    assert remote["last_sale_at"] == "2026-07-10"
    assert remote["average_daily_demand"] == 1
    assert other["sales_history_available"] is True
    assert other["demand_available"] is True
    assert other["has_observed_sales"] is False
    assert other["units_sold_observed"] == 0


def test_inventory_snapshot_does_not_merge_ambiguous_product_names() -> None:
    snapshots = extract_product_snapshots(
        [
            {"sku": "A", "name": "Filtro", "stock": 1},
            {"sku": "B", "name": "Filtro", "stock": 2},
        ],
        [
            {
                "document_id": 1,
                "issue_date": "2026-07-10",
                "details": [{"line_description": "Filtro", "quantity": 4}],
            }
        ],
    )

    assert [row["payload"]["units_sold_observed"] for row in snapshots] == [0, 0]


def test_inventory_snapshot_reads_facto_price_list_from_product_detail() -> None:
    snapshots = extract_product_snapshots(
        [
            {
                "product_id": 450,
                "sku": "RBA-450",
                "name": "Soporte de techo",
                "cost": {"currency_id": None, "value": "13236"},
                "price": [
                    {
                        "product_price_list_id": "1",
                        "unit_net": "25740.000000",
                        "unit_tax": 4890.6,
                        "unit_total": "30630.600000",
                        "currency_id": "39",
                    }
                ],
                "inventories": {
                    "total_available": 0,
                    "details": [
                        {"product_location_id": "1", "available_quantity": 0},
                        {"product_location_id": "2", "available_quantity": 96},
                    ],
                },
            }
        ],
        [],
    )

    snapshot = snapshots[0]["payload"]
    assert snapshot["available_units"] == 96.0
    assert snapshot["unit_price"] == 25740.0
    assert snapshot["unit_price_source"] == 25740.0
    assert snapshot["unit_price_is_net"] is True
    assert snapshot["price_known"] is True
    assert snapshot["price_currency_id"] == "39"
    assert snapshot["unit_margin"] == 12504.0


async def test_load_facto_details_expands_list_rows() -> None:
    class Client:
        async def product(self, product_id):
            return {"product_id": product_id, "sku": "P-1", "inventories": {"total_available": 7}}

        async def document(self, document_id):
            return {
                "document_id": document_id,
                "header": {"issue_date": "2026-07-01"},
                "details": [{"sku": "P-1", "quantity": 2}],
            }

    products, documents = await load_facto_details(
        Client(),
        {"products": [{"product_id": 1, "sku": "P-1"}]},
        {"documents": [{"document_id": 2}]},
    )

    assert products[0]["inventories"]["total_available"] == 7
    assert documents[0]["details"][0]["sku"] == "P-1"


async def test_load_paginated_records_reads_all_pages() -> None:
    async def loader(*, page: int):
        pages = {
            1: {"products": [{"id": 1}, {"id": 2}]},
            2: {"products": [{"id": 3}, {"id": 4}]},
            3: {"products": [{"id": 5}]},
        }
        return pages.get(page, {"products": []})

    rows = await load_paginated_records(loader, row_keys=("products",))

    assert [row["id"] for row in rows] == [1, 2, 3, 4, 5]


async def test_load_facto_sales_documents_filters_and_paginates_since_2025() -> None:
    class Client:
        calls = []

        async def documents(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["page"] == 1:
                return {
                    "documents": [
                        {"document_id": 1, "document_type_id": 2},
                        {"document_id": 2, "document_type_id": 9},
                        {"document_id": 3, "document_type_id": 37},
                        {"document_id": 4, "document_type_id": 54},
                    ],
                    "page": 1,
                    "page_count": 2,
                }
            return {
                "documents": [{"document_id": 5, "document_type_id": 28}],
                "page": 2,
                "page_count": 2,
            }

    client = Client()
    rows = await load_facto_sales_documents(client)

    assert [row["document_id"] for row in rows] == [1, 3, 5]
    assert len(client.calls) == 2
    assert client.calls[0]["per_page"] == 100
    assert client.calls[0]["document_status"] == 1
    assert client.calls[0]["issue_date_from"] == "2025-01-01"


async def test_load_paginated_records_stops_on_repeated_provider_page() -> None:
    async def loader(*, page: int):
        return {"products": [{"id": 1}, {"id": 2}]}

    rows = await load_paginated_records(loader, row_keys=("products",))

    assert len(rows) == 2
