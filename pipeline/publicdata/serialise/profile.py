"""The Parquet profile (docs/adr/0008-parquet-is-the-base-format.md).

It lives outside writers/, so it is build code that no cache key reads: a change here that
alters a version's data.parquet or its query copy raises a rebuild number, and the real-data
check (`publicdata verify`) compares both.

A published file never changes (ADR 0002), so a version's data.parquet follows the layout its
manifest records from the fetch that made it (`Manifest.parquet`), and a version fetched before
the profile keeps the writer it was published with. Every table version also gets a query copy
under the current profile and register entry, at an internal key (`query_key`).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import dumps

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from publicdata.normalise import Table
    from publicdata.provenance import Header
    from publicdata.register import Dataset

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


class Layout(TypedDict, total=False):
    """A layout as a manifest records it; empty for the legacy layout."""

    profile: str
    sort: list[str]
    key: list[str]
    lookup: list[str]
    int32: list[str]


def layout(ds: Dataset) -> Layout:
    """The register entry's layout under the current profile, as a manifest records it."""
    return {
        "profile": VERSION,
        "sort": list(ds.sort),
        "key": list(ds.key),
        "lookup": list(ds.lookup),
        "int32": list(ds.int32),
    }


def query_key(slug: str, version: str) -> str:
    """A version's query copy.

    A later profile writes beside it, so a reader of one profile never meets a file of another
    under the same key.
    """
    tag = "" if VERSION == "1" else f".p{VERSION}"
    return f"{QUERY_DIR}/{slug}/{version}{tag}.parquet"


def layout_key(query: str) -> str:
    """Where R2 records, beside a query copy, the layout the copy follows (`layout_body`)."""
    return query.removesuffix(".parquet") + ".layout.json"


def layout_body(lay: Layout) -> bytes:
    return json.dumps(lay, sort_keys=True, separators=(",", ":")).encode()


def sort_columns(sort: Sequence[str], key: Sequence[str]) -> list[str]:
    """The columns a sorted file is ordered by: the declared sort, then the key."""
    return [*sort, *(k for k in key if k not in sort)]


def permutation(t: pa.Table, sort: Sequence[str], key: Sequence[str]) -> pa.Array[Any] | None:
    """The source positions of the rows of `t` in profile order.

    It is None when no sort is declared and the rows keep the publisher's order. Ties after the
    sort and the key fall to the source position, so the order is complete. DuckDB sorts, since
    it spills to disk.
    """
    if not sort:
        return None

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


def order_of(tbl: Table, sort: Sequence[str], key: Sequence[str]) -> pa.Array[Any] | None:
    """The permutation for the rows of `tbl`, reused when the build has already worked it out.

    It is taken from the one the build worked out for this table and this sort when it has it,
    so a version is sorted once.
    """
    known: tuple[pa.Table, tuple[tuple[str, ...], tuple[str, ...]], pa.Array[Any]] | None = getattr(
        tbl, "order", None
    )
    if known and known[0] is tbl.table and known[1] == (tuple(sort), tuple(key)):
        return known[2]
    return permutation(tbl.table, sort, key)


def apply(t: pa.Table, perm: pa.Array[Any] | None) -> pa.Table:
    if perm is None or pc.all(pc.equal(perm, pa.array(range(len(perm)), pa.int64()))).as_py():
        return t  # a source already in order is not copied
    return t.take(perm)


def ordered(t: pa.Table, sort: Sequence[str], key: Sequence[str]) -> pa.Table:
    return apply(t, permutation(t, sort, key))


def misfits(t: pa.Table, cols: Sequence[str]) -> list[str]:
    """Each named integer column holding a value outside 32 bits, said with its range.

    The fetch asks before it stores a version, and register validate asks of the versions held.
    """
    out: list[str] = []
    for c in cols:
        if c not in t.column_names:
            continue
        mm: dict[str, Any] = pc.min_max(t.column(c)).as_py()  # type: ignore[assignment]  # pyarrow-stubs 20 gives a struct's as_py as a list
        if mm["min"] is not None and not (INT32[0] <= mm["min"] and mm["max"] <= INT32[1]):
            out.append(f"{c} holds {mm['min']} to {mm['max']}, outside 32 bits")
    return out


def int32_schema(t: pa.Table, cols: Sequence[str]) -> pa.Schema:
    """The schema of `t` with the named integer columns as INT32.

    A value outside 32 bits stops the build; the fetch holds such a version back, so this means a
    field declared since.
    """
    bad = misfits(t, cols)
    if bad:
        raise ValueError("; ".join(bad) + "; take it out of int32")
    return pa.schema(
        [f.with_type(pa.int32()) if f.name in cols else f for f in t.schema],
        metadata=t.schema.metadata,  # type: ignore[arg-type]  # pyarrow-stubs 20 asks for a dict of str or bytes and gets one of bytes
    )


def narrow(t: pa.Table, cols: Sequence[str]) -> pa.Table:
    return t.cast(int32_schema(t, cols)) if cols else t


def chunks(
    t: pa.Table, perm: pa.Array[Any] | None, rows: int = ROW_GROUP_ROWS
) -> Iterator[pa.Table]:
    """The rows of `t` in the order perm gives, a slice at a time, so a sort never copies it."""
    if perm is not None and pc.all(pc.equal(perm, pa.array(range(len(perm)), pa.int64()))).as_py():
        perm = None
    for i in range(0, t.num_rows, rows):
        yield t.slice(i, rows) if perm is None else t.take(perm.slice(i, rows))


def _normalised(t: pa.DataType) -> pa.DataType:
    if pa.types.is_int32(t):
        return pa.int64()
    # Parquet has no seconds unit, so a datetime comes back in milliseconds.
    if pa.types.is_timestamp(t) and t.unit != "s":
        return pa.timestamp("s", t.tz)
    return t


def widen(t: pa.Table) -> pa.Table:
    """A Parquet file's rows typed as normalise types them: every integer 64 bits and every
    datetime in seconds."""
    if all(_normalised(f.type) == f.type for f in t.schema):
        return t
    schema = pa.schema(
        [f.with_type(_normalised(f.type)) for f in t.schema],
        metadata=t.schema.metadata,  # type: ignore[arg-type]  # pyarrow-stubs 20 asks for a dict of str or bytes and gets one of bytes
    )
    return t.cast(schema)


def options(
    schema: pa.Schema,
    sorted_by: Sequence[str] = (),
    lookup: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Writer options for a file in profile order, written ROW_GROUP_ROWS rows to a group.

    `sorted_by` names the columns a sorted file is ordered by, and `lookup` maps each lookup
    field to its count of distinct values.
    """
    opts: dict[str, Any] = {
        "compression": "zstd",
        "use_dictionary": True,
        "write_statistics": True,
        "max_rows_per_page": PAGE_ROWS,
        "data_page_size": PAGE_BYTES,
        "write_page_index": bool(sorted_by),
    }
    if sorted_by:
        opts["sorting_columns"] = pq.SortingColumn.from_ordering(
            schema, [(c, "ascending") for c in sorted_by], null_placement="at_end"
        )
    if lookup:
        opts["bloom_filter_options"] = {
            c: {"ndv": max(1, min(n, ROW_GROUP_ROWS)), "fpp": BLOOM_FPP} for c, n in lookup.items()
        }
    return opts


def metadata(header: Header, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {"publicdata": dumps(header), KEY: VERSION, **(extra or {})}


def write(  # noqa: PLR0913 - the options are keyword-only and named at each call
    t: pa.Table,
    header: Header,
    path: Path,
    lay: Layout,
    perm: pa.Array[Any] | None = None,
    *,
    extra: dict[str, str] | None = None,
) -> None:
    """Write `t` as one profile file at path under layout `lay`.

    `perm` is the order of `t` when the caller has it.
    """
    if lay.get("profile") != VERSION:
        # A version keeps the profile it was published under, so a later one keeps this writer.
        msg = f"no writer for Parquet profile {lay.get('profile')!r}"
        raise ValueError(msg)
    sort, key = lay.get("sort", ()), lay.get("key", ())
    if perm is None:
        perm = permutation(t, sort, key)
    schema = int32_schema(t, lay.get("int32", ()))
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


def follows(meta: pq.FileMetaData, lay: Layout) -> bool:
    """Whether a Parquet footer shows layout `lay`.

    The layout is its profile key, the sorting columns of every row group, the fields with a
    bloom filter and the fields written as INT32.
    """
    if (meta.metadata or {}).get(KEY.encode()) != lay["profile"].encode():
        return False
    names = [meta.schema.column(i).name for i in range(meta.num_columns)]
    narrow = {f.name for f in meta.schema.to_arrow_schema() if pa.types.is_int32(f.type)}
    if narrow != {c for c in lay["int32"] if c in names}:
        return False
    order = (
        [(c, False, False) for c in sort_columns(lay["sort"], lay["key"])] if lay["sort"] else []
    )
    for g in range(meta.num_row_groups):
        rg = meta.row_group(g)
        got = [
            (names[c.column_index], c.descending, c.nulls_first) for c in rg.sorting_columns or ()
        ]
        blooms = {
            names[i]
            for i in range(meta.num_columns)
            if rg.column(i).bloom_filter_offset is not None  # type: ignore[attr-defined]  # pyarrow-stubs 20 predates it
        }
        if got != order or blooms != set(lay["lookup"]):
            return False
    return True


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
