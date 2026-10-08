from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from publicdata.serialise import (
    DUCKDB_TYPES,
    duckdb_comment,
    duckdb_connect,
    duckdb_meta,
    field_rows,
    profile,
)

if TYPE_CHECKING:
    from pathlib import Path

    from publicdata.normalise import Table


def write_duckdb(tbl: Table, header: dict, path: Path) -> None:
    """One DuckDB database: a `records` table with the typed columns, plus the `fields` and
    `publicdata` tables. The file attaches read-only over HTTPS, so a query can run against it
    without a download. Its rows are in the Parquet's order.
    """
    ds = tbl.dataset
    con = duckdb_connect(path, tbl.rows)
    try:
        cols = [f'"{f.name}" {DUCKDB_TYPES[f.type]}' for f in ds.fields]
        if "suppressed" in tbl.table.column_names:
            cols.append('"suppressed" VARCHAR[]')
        con.execute(f"CREATE TABLE records ({', '.join(cols)})")
        lay = tbl.manifest.parquet
        perm = profile.order_of(tbl, lay["sort"], lay["key"]) if lay else None
        # One insert: DuckDB writes a larger file when the rows arrive in several.
        src = tbl.table
        if perm is not None:
            parts = (b for part in profile.chunks(src, perm) for b in part.to_batches())
            src = pa.RecordBatchReader.from_batches(src.schema, parts)
        con.register("src", src)
        con.execute("INSERT INTO records SELECT * FROM src")
        con.unregister("src")
        duckdb_comment(con, "records", None, ds.title)
        for f in ds.fields:
            duckdb_comment(con, "records", f.name, f.description or f.source)
        duckdb_meta(con, header, field_rows(tbl))
        con.execute("CHECKPOINT")
    finally:
        con.close()
