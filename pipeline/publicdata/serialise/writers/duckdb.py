from __future__ import annotations

from pathlib import Path

from ...normalise import Table
from .. import DUCKDB_TYPES, duckdb_comment, duckdb_connect, duckdb_meta, field_rows, profile


def write_duckdb(tbl: Table, header: dict, path: Path) -> None:
    """One DuckDB database: a `records` table with the typed columns, plus the `fields` and
    `publicdata` tables. The file attaches read-only over HTTPS, so a query can run against it
    without a download. Its rows are in the Parquet's order."""
    ds = tbl.dataset
    con = duckdb_connect(path, tbl.rows)
    try:
        cols = [f'"{f.name}" {DUCKDB_TYPES[f.type]}' for f in ds.fields]
        if "suppressed" in tbl.table.column_names:
            cols.append('"suppressed" VARCHAR[]')
        con.execute(f"CREATE TABLE records ({', '.join(cols)})")
        con.register("src", profile.ordered(tbl.table, ds.sort, ds.key))
        con.execute("INSERT INTO records SELECT * FROM src")
        con.unregister("src")
        duckdb_comment(con, "records", None, ds.title)
        for f in ds.fields:
            duckdb_comment(con, "records", f.name, f.description or f.source)
        duckdb_meta(con, header, field_rows(tbl))
        con.execute("CHECKPOINT")
    finally:
        con.close()
