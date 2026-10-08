from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from publicdata.normalise import Table
from publicdata.serialise import dumps, iter_rows, json_view, table_schema


def write_json(tbl: Table, header: dict, path: Path, rows: pa.Table | None = None) -> None:
    t = json_view(rows if rows is not None else tbl.table)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write('{"publicdata":')
        f.write(dumps(header))
        f.write(',"fields":')
        f.write(dumps(table_schema(tbl)["fields"]))
        f.write(',"records":[')
        first = True
        for row in iter_rows(t):
            if not first:
                f.write(",")
            first = False
            f.write("\n")
            f.write(dumps(row))
        f.write("\n]}\n")
