from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from publicdata.serialise import dumps, profile

if TYPE_CHECKING:
    from pathlib import Path

    from publicdata.normalise import Table


def write_parquet(
    tbl: Table,
    header: dict,
    path: Path,
    lay: dict | None = None,
) -> None:
    """The version's Parquet under the layout its manifest records, or `lay` for a query copy.
    A version fetched before the profile records none and keeps the writer it was published
    with.
    """
    lay = tbl.manifest.parquet if lay is None else lay
    if not lay:
        t = tbl.table.replace_schema_metadata({"publicdata": dumps(header)})
        pq.write_table(t, path, compression="zstd", write_statistics=True, row_group_size=65_536)
        return
    perm = profile.order_of(tbl, lay["sort"], lay["key"])
    profile.write(tbl.table, header, path, lay, perm)
