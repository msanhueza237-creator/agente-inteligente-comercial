import io

import pandas as pd


def read_workbook_tables(content: bytes, filename: str) -> list[tuple[str, pd.DataFrame]]:
    """Read every worksheet (or the single CSV table) as raw strings."""
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xls")):
        workbook = pd.read_excel(io.BytesIO(content), dtype=str, sheet_name=None)
        tables = [(str(sheet).strip() or "Hoja", frame) for sheet, frame in workbook.items()]
    elif lower.endswith(".csv"):
        frame = pd.read_csv(io.BytesIO(content), dtype=str, sep=None, engine="python")
        tables = [("CSV", frame)]
    else:
        raise ValueError(f"Formato de archivo no soportado: {filename}")

    cleaned: list[tuple[str, pd.DataFrame]] = []
    for sheet, frame in tables:
        frame.columns = [str(column).strip() for column in frame.columns]
        cleaned.append((sheet, frame.where(pd.notnull(frame), None)))
    return cleaned


def read_table(content: bytes, filename: str) -> pd.DataFrame:
    """Load an uploaded Excel/CSV file into a DataFrame of raw strings.
    Client files vary in format, so this stays deliberately permissive:
    empty cells become None, everything else is read as-is (normalization
    happens later, per-field, in the pipeline).
    """
    tables = read_workbook_tables(content, filename)
    if not tables:
        return pd.DataFrame()
    return pd.concat([frame for _, frame in tables], ignore_index=True, sort=False)


def rows_from_mapping(df: pd.DataFrame, mapping: dict[str, str | None]) -> list[dict]:
    """Project the DataFrame into a list of {prospect_field: raw_value}
    dicts using the resolved column mapping. Unmapped fields are omitted.
    """
    active_mapping = {field: header for field, header in mapping.items() if header}

    rows: list[dict] = []
    for _, series in df.iterrows():
        row: dict = {}
        for field, header in active_mapping.items():
            value = series.get(header)
            if value is not None:
                value = str(value).strip() or None
            row[field] = value
        if any(row.values()):
            rows.append(row)
    return rows
