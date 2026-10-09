from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from publicdata.serialise import SQLITE_TYPES, _meta_tables, json_view

if TYPE_CHECKING:
    from pathlib import Path

    from publicdata.normalise import Table
    from publicdata.provenance import Header


def write_sqlite(tbl: Table, header: Header, path: Path) -> None:
    if path.exists():
        path.unlink()
    ds = tbl.dataset
    names = [f.name for f in ds.fields]
    cols = [f'"{f.name}" {SQLITE_TYPES[f.type]}' for f in ds.fields]
    if "suppressed" in tbl.table.column_names:
        names.append("suppressed")
        cols.append('"suppressed" TEXT')
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(f"CREATE TABLE records ({', '.join(cols)})")
    _meta_tables(con, tbl, header)
    q = f"INSERT INTO records VALUES ({', '.join('?' * len(names))})"
    t = json_view(tbl.table)
    for b in t.to_batches(20_000):
        rows = []
        for row in b.to_pylist():
            if "suppressed" in row and row["suppressed"] is not None:
                row["suppressed"] = ";".join(row["suppressed"]) or None
            rows.append(tuple(row[n] for n in names))
        con.executemany(q, rows)
    if ds.key:
        keycols = ", ".join(f'"{k}"' for k in ds.key)
        con.execute(f"CREATE INDEX records_key ON records ({keycols})")
    con.commit()
    con.execute("VACUUM")
    con.close()
