"""The Parquet profile every Parquet file follows (docs/adr/0008-parquet-is-the-base-format.md).
It lives outside writers/ so the build cache keys every version on it: a change here rebuilds
every version, as a change to the profile must."""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import dumps

VERSION = "1"
# The footer key a reader checks before it relies on the order, the sizes and the page index.
KEY = "publicdata.profile"
ROW_GROUP_ROWS = 500_000
PAGE_ROWS = 10_000
# The byte limit sits above any 10,000-row page, so the row count decides where a page ends.
PAGE_BYTES = 64 * 1024 * 1024
BLOOM_FPP = 0.01
INT32 = (-(2**31), 2**31 - 1)
POSITION = "__position"


def sort_columns(sort: Sequence[str], key: Sequence[str]) -> list[str]:
    """The columns a sorted file is ordered by: the declared sort, then the key."""
    return [*sort, *(k for k in key if k not in sort)]


def permutation(t: pa.Table, sort: Sequence[str], key: Sequence[str]) -> pa.Array | None:
    """The source positions of t's rows in profile order, or None when no sort is declared and
    the rows keep the publisher's order. Ties after the sort and the key fall to the source
    position, so the order is complete. DuckDB sorts, since it spills to disk."""
    if not sort:
        return None
    import duckdb

    cols = sort_columns(sort, key)
    keys = t.select(cols).append_column(POSITION, pa.array(range(t.num_rows), pa.int64()))
    by = ", ".join('"' + c.replace('"', '""') + '" ASC NULLS LAST' for c in cols)
    with tempfile.TemporaryDirectory() as tmp:
        con = duckdb.connect()
        try:
            con.execute(f"SET temp_directory = '{Path(tmp).as_posix()}'")
            con.execute("SET memory_limit = '2GB'")
            con.register("rows_in", keys)
            out = con.execute(
                f"SELECT {POSITION} FROM rows_in ORDER BY {by}, {POSITION}"
            ).to_arrow_table()
        finally:
            con.close()
    return out.column(POSITION).combine_chunks()


def ordered(t: pa.Table, sort: Sequence[str], key: Sequence[str]) -> pa.Table:
    perm = permutation(t, sort, key)
    if perm is None or pc.all(pc.equal(perm, pa.array(range(len(perm)), pa.int64()))).as_py():
        return t  # a source already in order is not copied
    return t.take(perm)


def int32_columns(t: pa.Table) -> list[str]:
    """The 64-bit integer columns whose values all fit 32 bits."""
    out = []
    for name in t.column_names:
        c = t.column(name)
        if not pa.types.is_int64(c.type):
            continue
        mm = pc.min_max(c).as_py()
        if mm["min"] is None or (INT32[0] <= mm["min"] and mm["max"] <= INT32[1]):
            out.append(name)
    return out


def narrow(t: pa.Table, cols: Sequence[str] | None = None) -> pa.Table:
    cols = int32_columns(t) if cols is None else cols
    if not cols:
        return t
    schema = pa.schema(
        [f.with_type(pa.int32()) if f.name in cols else f for f in t.schema],
        metadata=t.schema.metadata,
    )
    return t.cast(schema)


def widen(t: pa.Table) -> pa.Table:
    """A profile file's rows typed as normalise types them, every integer 64 bits."""
    if not any(pa.types.is_int32(f.type) for f in t.schema):
        return t
    schema = pa.schema(
        [f.with_type(pa.int64()) if pa.types.is_int32(f.type) else f for f in t.schema],
        metadata=t.schema.metadata,
    )
    return t.cast(schema)


def options(
    schema: pa.Schema,
    sorted_by: Sequence[str] = (),
    lookup: dict[str, int] | None = None,
) -> dict:
    """Writer options for a file in profile order, written ROW_GROUP_ROWS rows to a group.
    `sorted_by` names the columns a sorted file is ordered by, and `lookup` maps each lookup
    field to its count of distinct values."""
    opts = dict(
        compression="zstd",
        use_dictionary=True,
        write_statistics=True,
        max_rows_per_page=PAGE_ROWS,
        data_page_size=PAGE_BYTES,
        write_page_index=bool(sorted_by),
    )
    if sorted_by:
        opts["sorting_columns"] = pq.SortingColumn.from_ordering(
            schema, [(c, "ascending") for c in sorted_by], null_placement="at_end"
        )
    if lookup:
        opts["bloom_filter_options"] = {
            c: {"ndv": max(1, min(n, ROW_GROUP_ROWS)), "fpp": BLOOM_FPP} for c, n in lookup.items()
        }
    return opts


def metadata(header: dict, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {"publicdata": dumps(header), KEY: VERSION, **(extra or {})}


def write(
    t: pa.Table,
    header: dict,
    path: Path,
    sort: Sequence[str] = (),
    key: Sequence[str] = (),
    lookup: Sequence[str] = (),
    int32: Sequence[str] | None = None,
    extra: dict[str, str] | None = None,
) -> None:
    """t as one profile file at path. `int32` names the columns written as INT32; None works
    them out from t's own values, so a caller writing one table in pieces passes the whole
    table's."""
    t = narrow(ordered(t, sort, key), int32)
    counts = {c: pc.count_distinct(t.column(c)).as_py() for c in lookup}
    sorted_by = sort_columns(sort, key) if sort else []
    t = t.replace_schema_metadata(metadata(header, extra))
    pq.write_table(t, path, row_group_size=ROW_GROUP_ROWS, **options(t.schema, sorted_by, counts))
