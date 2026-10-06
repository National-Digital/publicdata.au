"""The Parquet profile (docs/adr/0008-parquet-is-the-base-format.md). It lives outside writers/ so
the build cache keys every version on it: a change here rebuilds every version.

A published file never changes (ADR 0002), so a version's data.parquet follows the layout its
manifest records from the fetch that made it (`Manifest.parquet`), and a version fetched before
the profile keeps the writer it was published with. Every table version also gets a query copy
under the current profile and register entry, at an internal key (`query_key`)."""

from __future__ import annotations

import hashlib
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
# A page ends at PAGE_ROWS or at this many bytes, whichever comes first: short columns reach the
# row count, and long text or geometry the byte cap.
PAGE_BYTES = 8 * 1024 * 1024
BLOOM_FPP = 0.01
INT32 = (-(2**31), 2**31 - 1)
POSITION = "__position"
# Query copies sit under this prefix in publicdata-dist, outside the URL contract.
QUERY_DIR = "_q"
# What a layout records. The legacy layout, {}, is the writer before the profile.
LAYOUT_KEYS = ("profile", "sort", "key", "lookup", "int32")


def layout(ds) -> dict:
    """The register entry's layout under the current profile, as a manifest records it."""
    return {
        "profile": VERSION,
        "sort": list(ds.sort),
        "key": list(ds.key),
        "lookup": list(ds.lookup),
        "int32": list(ds.int32),
    }


def query_key(slug: str, version: str) -> str:
    """A version's query copy. A later profile writes beside it, so a reader of one profile
    never meets a file of another under the same key."""
    tag = "" if VERSION == "1" else f".p{VERSION}"
    return f"{QUERY_DIR}/{slug}/{version}{tag}.parquet"


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
            con.execute("SET memory_limit = '1GB'")
            con.register("rows_in", keys)
            out = con.execute(
                f"SELECT {POSITION} FROM rows_in ORDER BY {by}, {POSITION}"
            ).to_arrow_table()
        finally:
            con.close()
    return out.column(POSITION).combine_chunks()


def order_of(tbl, sort: Sequence[str], key: Sequence[str]) -> pa.Array | None:
    """The permutation for tbl's rows, taken from the one the build worked out for this table
    and this sort when it has it, so a version is sorted once."""
    known = getattr(tbl, "order", None)
    if known and known[0] is tbl.table and known[1] == (tuple(sort), tuple(key)):
        return known[2]
    return permutation(tbl.table, sort, key)


def apply(t: pa.Table, perm: pa.Array | None) -> pa.Table:
    if perm is None or pc.all(pc.equal(perm, pa.array(range(len(perm)), pa.int64()))).as_py():
        return t  # a source already in order is not copied
    return t.take(perm)


def ordered(t: pa.Table, sort: Sequence[str], key: Sequence[str]) -> pa.Table:
    return apply(t, permutation(t, sort, key))


def int32_schema(t: pa.Table, cols: Sequence[str]) -> pa.Schema:
    """t's schema with the named integer columns as INT32. A value outside 32 bits stops the
    build, since the register promised every version of the field fits."""
    for c in cols:
        mm = pc.min_max(t.column(c)).as_py()
        if mm["min"] is not None and not (INT32[0] <= mm["min"] and mm["max"] <= INT32[1]):
            raise ValueError(
                f"{c} holds {mm['min']} to {mm['max']}, outside 32 bits; take it out of int32"
            )
    return pa.schema(
        [f.with_type(pa.int32()) if f.name in cols else f for f in t.schema],
        metadata=t.schema.metadata,
    )


def narrow(t: pa.Table, cols: Sequence[str]) -> pa.Table:
    return t.cast(int32_schema(t, cols)) if cols else t


def chunks(t: pa.Table, perm: pa.Array | None, rows: int = ROW_GROUP_ROWS):
    """t's rows in the order perm gives, a slice at a time, so a sort never copies the table."""
    if perm is not None and pc.all(pc.equal(perm, pa.array(range(len(perm)), pa.int64()))).as_py():
        perm = None
    for i in range(0, t.num_rows, rows):
        yield t.slice(i, rows) if perm is None else t.take(perm.slice(i, rows))


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
    lay: dict,
    perm: pa.Array | None = None,
    int32: Sequence[str] | None = None,
    extra: dict[str, str] | None = None,
) -> None:
    """t as one profile file at path under layout `lay`. `perm` is t's order when the caller has
    it; `int32` overrides the layout's INT32 fields."""
    if lay.get("profile") != VERSION:
        # A version keeps the profile it was published under, so a later one keeps this writer.
        raise ValueError(f"no writer for Parquet profile {lay.get('profile')!r}")
    sort, key = lay.get("sort", ()), lay.get("key", ())
    if perm is None:
        perm = permutation(t, sort, key)
    schema = int32_schema(t, lay.get("int32", ()) if int32 is None else int32)
    schema = schema.with_metadata(metadata(header, extra))
    counts = {c: pc.count_distinct(t.column(c)).as_py() for c in lay.get("lookup", ())}
    sorted_by = sort_columns(sort, key) if sort else []
    with pq.ParquetWriter(path, schema, **options(schema, sorted_by, counts)) as w:
        for part in chunks(t, perm):
            w.write_table(part.cast(schema), row_group_size=ROW_GROUP_ROWS)


def signature(path: Path) -> str:
    """The order a Parquet file's rows are in: its sorting columns, or "" for the publisher's."""
    meta = pq.read_metadata(path)
    if KEY.encode() not in (meta.metadata or {}) or not meta.num_row_groups:
        return ""
    cols = meta.row_group(0).sorting_columns or ()
    return ",".join(meta.schema.column(c.column_index).name for c in cols)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
