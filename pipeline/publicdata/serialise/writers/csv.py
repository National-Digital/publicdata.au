from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.csv as pcsv

if TYPE_CHECKING:
    from pathlib import Path

    from publicdata.normalise import Table


def write_csv(tbl: Table, path: Path) -> None:
    t = tbl.table
    if "suppressed" in t.column_names:
        i = t.column_names.index("suppressed")
        joined = pa.array([";".join(v) if v else None for v in t.column(i).to_pylist()])
        t = t.set_column(i, "suppressed", joined)
    pcsv.write_csv(t, path, pcsv.WriteOptions(include_header=True, quoting_style="needed"))
