from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from publicdata.serialise import dumps

if TYPE_CHECKING:
    from pathlib import Path

    from publicdata.normalise import Table


def write_arrow(tbl: Table, header: dict, path: Path) -> None:
    """Arrow IPC file (Feather v2), zstd compressed, provenance in the schema metadata."""
    t = tbl.table.replace_schema_metadata({"publicdata": dumps(header)})
    with pa.OSFile(str(path), "wb") as sink:
        opts = pa.ipc.IpcWriteOptions(compression="zstd")
        with pa.ipc.new_file(sink, t.schema, options=opts) as w:
            for b in t.to_batches(65_536):
                w.write_batch(b)
