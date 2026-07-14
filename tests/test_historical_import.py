from io import BytesIO

import pandas as pd

from app.ingestion.historical import normalize_historical_file


def test_sample_columns_are_normalized_without_creating_people():
    content = (
        "Nombre Vendedor;Código;Razón social;Rut;Email;Teléfono 1\n"
        "SIN VENDEDOR ASIGNADO;76665976;SLK SPA;76.665.976-4;mcerda@slk.cl;\n"
        "SIN VENDEDOR ASIGNADO;77521180;GRUPO SINGER Y SINGER S.A.;77.521.180-6;"
        "imad.singer@gruposinger.cl;94115637\n"
    ).encode()

    result = normalize_historical_file(content, "clientes.csv")

    assert result.stats["entities"] == 2
    assert result.rows[0]["legal_name"] == "SLK SPA"
    assert result.rows[0]["rut_normalized"] == "76665976-4"
    assert result.rows[0]["emails"] == ["mcerda@slk.cl"]
    assert "telefono_ambiguo" not in result.rows[0]["flags"]
    assert "seller" not in result.rows[0]
    assert result.rows[1]["phone_normalized"] == ""
    assert "telefono_ambiguo" in result.rows[1]["flags"]
    assert result.rows[1]["territory_status"] == "unknown"


def test_all_excel_sheets_are_consolidated_with_provenance():
    output = BytesIO()
    columns = ["Nombre Vendedor", "Código", "Razón social", "Rut", "Email", "Teléfono 1"]
    first = pd.DataFrame(
        [["A", "123", "EMPRESA REPETIDA", "", "ventas@empresa.cl", ""]], columns=columns
    )
    second = pd.DataFrame(
        [["B", "123", "EMPRESA REPETIDA", "", "contacto@empresa.cl", ""]], columns=columns
    )
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        first.to_excel(writer, sheet_name="Vendedor A", index=False)
        second.to_excel(writer, sheet_name="Vendedor B", index=False)

    result = normalize_historical_file(output.getvalue(), "clientes.xlsx")

    assert result.sheets == ["Vendedor A", "Vendedor B"]
    assert result.stats["source_rows"] == 2
    assert result.stats["entities"] == 1
    assert result.stats["duplicates_consolidated"] == 1
    assert result.rows[0]["emails"] == ["ventas@empresa.cl", "contacto@empresa.cl"]
    assert result.rows[0]["provenance"] == [
        {"sheet": "Vendedor A", "row": 2},
        {"sheet": "Vendedor B", "row": 2},
    ]
