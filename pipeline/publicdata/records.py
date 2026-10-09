"""A version's rows for the build's own queries: DuckDB over its data.parquet, or its period parts.

The rows are shaped as the records table of its data.sqlite. Dates are ISO text, booleans 1 and
0, the suppressed flags joined with ";", a float's NaN a null, a layer's shapes left out, and
rowid is the row's place in the file, or in the parts one after another. The pages' figures, the
query console and the D1 load read the Parquet alone, and answer as SQLite did, in the Parquet's
row order.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Self

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


# A row of a query's answer. DuckDB types its rows' values as Any, and each caller knows from its
# own SQL what a column holds, so a row is named once here.
type Row = tuple[Any, ...]  # type: ignore[explicit-any]  # duckdb types a fetched row's values as Any


class _Cursor(Protocol):
    def fetchone(self) -> Row | None: ...


# A value as SQLite holds it.
type SQLValue = str | int | float | bytes | None

ROWID = "rowid"
# SQLite sums floats with Kahan-Babuska-Neumaier compensation, in row order, and averages the
# compensated sum. This sums the same way in the Parquet's row order, so a total or a mean agrees
# with data.sqlite to the last bit when the Parquet keeps the publisher's order; a sorted Parquet
# sums in its own order, and the last bit can differ.
# The lambdas' names cannot be a column's, which would shadow them.
_PAIRS = "list_transform(list({c} ORDER BY rowid) FILTER (WHERE {c} IS NOT NULL), kbn_x -> {{'s': kbn_x, 'c': 0.0::DOUBLE}})"
_KBN = (
    "list_reduce({pairs}, (kbn_a, kbn_x) -> {{'s': kbn_a.s + kbn_x.s, 'c': kbn_a.c + CASE"
    " WHEN abs(kbn_a.s) > abs(kbn_x.s) THEN (kbn_a.s - (kbn_a.s + kbn_x.s)) + kbn_x.s"
    " ELSE (kbn_x.s - (kbn_a.s + kbn_x.s)) + kbn_a.s END}},"
    " {{'s': 0.0::DOUBLE, 'c': 0.0::DOUBLE}})"
)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _column(name: str, t: pa.DataType) -> str:
    c = _q(name)
    if pa.types.is_floating(t):
        # SQLite stores a NaN as NULL, and a NaN would turn a sum, a minimum or a maximum.
        return f"CASE WHEN isnan({c}) THEN NULL ELSE {c} END AS {c}"
    if pa.types.is_date(t):
        return f"strftime({c}, '%Y-%m-%d') AS {c}"
    if pa.types.is_timestamp(t):
        return f"strftime({c}, '%Y-%m-%dT%H:%M:%S') AS {c}"
    if pa.types.is_boolean(t):
        return f"CAST({c} AS INTEGER) AS {c}"
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return f"NULLIF(array_to_string({c}, ';'), '') AS {c}"
    return c


@lru_cache(maxsize=4096, typed=True)
def _affinity(value: object, affinity: str) -> SQLValue:
    """The value as SQLite holds it in a column of that affinity."""
    with closing(sqlite3.connect(":memory:")) as s:
        s.execute(f"CREATE TABLE t (v {affinity})")
        s.execute("INSERT INTO t VALUES (?)", (value,))
        got: SQLValue = s.execute("SELECT v FROM t").fetchone()[0]
        return got


class Records:
    """A read-only connection whose `records` view holds one version's rows.

    A parameter compared with a column goes through param, so it compares as it would against
    data.sqlite. parquet is the version's file, or the list of its parts in order, whose rows
    follow one another and keep their order within each part.
    """

    def __init__(self, parquet: Path | list[Path], names: list[str] | None = None) -> None:
        files = [parquet] if isinstance(parquet, Path) else list(parquet)
        schema = pq.read_schema(files[0])
        have = [n for n in schema.names if n != "geometry"]
        # The SQLite file lists the register's fields in their order, then the flags.
        order = [n for n in names or () if n in have]
        self.names = order + [n for n in have if n not in order]
        self.con = duckdb.connect()
        # One thread, so an answer whose order SQL leaves open comes the same way every build.
        self.con.execute("SET threads = 1")
        self.con.execute("SET TimeZone = 'UTC'")
        self.con.execute("SET default_null_order = 'nulls_first_on_asc_last_on_desc'")
        types = {n: schema.field(n).type for n in self.names}
        self.floats = {n for n, t in types.items() if pa.types.is_floating(t)}
        self.numeric = {
            n
            for n, t in types.items()
            if pa.types.is_integer(t)
            or pa.types.is_floating(t)
            or pa.types.is_decimal(t)
            or pa.types.is_boolean(t)
        }
        cols = ", ".join(_column(n, schema.field(n).type) for n in self.names)
        selects: list[str] = []
        before = 0
        for f in files:
            path = str(f).replace("'", "''")
            selects.append(
                f"SELECT {cols}, file_row_number + {before + 1} AS {ROWID} "
                f"FROM read_parquet('{path}', file_row_number = true)"
            )
            if len(files) > 1:
                before += pq.read_metadata(f).num_rows
        self.con.execute(f"CREATE VIEW records AS {' UNION ALL BY NAME '.join(selects)}")

    def columns(self) -> list[str]:
        return list(self.names)

    def agg(self, fn: str, name: str) -> str:
        """count, sum, avg, min or max of a column as SQL that answers as SQLite's would."""
        c = _q(name)
        if fn in ("sum", "avg") and name in self.floats:
            r = _KBN.format(pairs=_PAIRS.format(c=c))
            # SQLite drops the error term once it is NaN, which an infinite value makes it.
            total = (
                f"CASE WHEN count({c}) = 0 THEN NULL WHEN isnan({r}.c) THEN {r}.s"
                f" ELSE {r}.s + {r}.c END"
            )
            return total if fn == "sum" else f"({total}) / count({c})"
        return f"{fn.upper()}({c})"

    def param(self, name: str, value: object) -> SQLValue:
        """The value as SQLite compares it with the column.

        Text that reads as a number becomes that number against a numeric column, and a number
        becomes text against a text one. Text that is not a number against a numeric column is
        refused, since SQLite would rank it above every number.
        """
        if value is None:
            return None
        if name not in self.numeric:
            return _affinity(value, "TEXT")
        v = _affinity(value, "NUMERIC")
        if isinstance(v, str):
            msg = f"{name} holds numbers, so it cannot be compared with {value!r}"
            raise ValueError(msg)  # noqa: TRY004 - the value does not fit the column
        return v

    def execute(self, sql: str, params: Iterable[object] = ()) -> Records:
        self.con.execute(sql, list(params))
        return self

    def fetchone(self) -> Row | None:
        return self.con.fetchone()

    def fetchall(self) -> list[Row]:
        return self.con.fetchall()

    def fetchmany(self, n: int) -> list[Row]:
        return self.con.fetchmany(n)

    def __iter__(self) -> Iterator[Row]:
        return iter(self.con.fetchall())

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def one_row(cur: _Cursor) -> Row:
    """The row an aggregate query answers, which it always does."""
    row = cur.fetchone()
    if row is None:
        msg = "an aggregate query answered no row"
        raise RuntimeError(msg)
    return row


def connect(parquet: Path | list[Path], names: list[str] | None = None) -> Records:
    return Records(parquet, names)


def joined(parts: list[Path], dest: Path) -> Path:
    """A version written as parts alone: its parts' rows in one Parquet file at dest.

    The rows come in the order given, for the queries its pages draw from. One part is read at a
    time, and a part written before a type narrowed is widened to meet the rest, as the build
    reads parts back.
    """
    if not parts:
        return dest
    schema = pa.unify_schemas(
        [pq.read_schema(p).remove_metadata() for p in parts], promote_options="permissive"
    )
    with pq.ParquetWriter(dest, schema, compression="zstd") as w:
        for p in parts:
            w.write_table(pq.read_table(p).replace_schema_metadata(None).cast(schema))
    return dest
