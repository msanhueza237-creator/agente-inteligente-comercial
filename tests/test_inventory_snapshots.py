from app.hub.inventory import eligible_replenishment_payloads, extract_product_snapshots


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
    assert snapshot["unit_cost_usd"] == 85.0
    assert snapshot["average_daily_demand"] == 1.0
