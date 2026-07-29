from app.hub.inventory import eligible_replenishment_payloads, extract_product_snapshots
from app.hub.worker import load_facto_details


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
    assert snapshot["average_daily_demand"] == 1.0
    assert snapshot["demand_available"] is True


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
