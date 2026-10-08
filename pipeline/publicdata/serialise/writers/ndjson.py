from __future__ import annotations

from typing import TYPE_CHECKING

from publicdata.serialise import dumps, iter_rows, json_view

if TYPE_CHECKING:
    from pathlib import Path

    from publicdata.normalise import Table


def write_ndjson(tbl: Table, header: dict, path: Path) -> None:
    t = json_view(tbl.table)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(dumps({"publicdata": header}) + "\n")
        for row in iter_rows(t):
            f.write(dumps(row) + "\n")
