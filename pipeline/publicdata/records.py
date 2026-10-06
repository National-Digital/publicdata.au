"""A version's rows for the build's own queries: DuckDB over its data.parquet, shaped as the
records table of its data.sqlite. Dates are ISO text, booleans 1 and 0, the suppressed flags
joined with ";", a layer's shapes left out, and rowid is the row's place in the file. The pages'
figures, the query console and the D1 load read the Parquet alone, and answer as SQLite did."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROWID = "rowid"
# SQLite sums floats with Kahan-Babuska-Neumaier compensation, in row order, and averages the
# compensated sum, so a total or a mean here agrees with it to the last bit.
_PAIRS = "list_transform(list({c} ORDER BY rowid) FILTER (WHERE {c} IS NOT NULL), x -> {{'s': x, 'c': 0.0::DOUBLE}})"
_KBN = (
    "list_reduce({pairs}, (a, x) -> {{'s': a.s + x.s, 'c': a.c + CASE WHEN abs(a.s) > abs(x.s)"
    " THEN (a.s - (a.s + x.s)) + x.s ELSE (x.s - (a.s + x.s)) + a.s END}},"
    " {{'s': 0.0::DOUBLE, 'c': 0.0::DOUBLE}})"
)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _column(name: str, t: pa.DataType) -> str:
    c = _q(name)
    if pa.types.is_date(t):
        return f"strftime({c}, '%Y-%m-%d') AS {c}"
    if pa.types.is_timestamp(t):
        return f"strftime({c}, '%Y-%m-%dT%H:%M:%S') AS {c}"
    if pa.types.is_boolean(t):
        return f"CAST({c} AS INTEGER) AS {c}"
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return f"NULLIF(array_to_string({c}, ';'), '') AS {c}"
    return c


class Records:
    """A read-only connection whose `records` view holds one version's rows. Parameters are bound
    as text, which DuckDB casts to the column's type, as SQLite's column affinity does."""

    def __init__(self, parquet: Path, names: list[str] | None = None):
        import duckdb

        schema = pq.read_schema(parquet)
        have = [n for n in schema.names if n != "geometry"]
        # The SQLite file lists the register's fields in their order, then the flags.
        order = [n for n in names or () if n in have]
        self.names = order + [n for n in have if n not in order]
        self.con = duckdb.connect()
        # One thread, so an answer whose order SQL leaves open comes the same way every build.
        self.con.execute("SET threads = 1")
        self.con.execute("SET TimeZone = 'UTC'")
        self.con.execute("SET default_null_order = 'nulls_first_on_asc_last_on_desc'")
        self.floats = {n for n in self.names if pa.types.is_floating(schema.field(n).type)}
        cols = ", ".join(_column(n, schema.field(n).type) for n in self.names)
        path = str(parquet).replace("'", "''")
        self.con.execute(
            f"CREATE VIEW records AS SELECT {cols}, file_row_number + 1 AS {ROWID} "
            f"FROM read_parquet('{path}', file_row_number = true)"
        )

    def columns(self) -> list[str]:
        return list(self.names)

    def agg(self, fn: str, name: str) -> str:
        """count, sum, avg, min or max of a column as SQL that answers as SQLite's would."""
        c = _q(name)
        if fn in ("sum", "avg") and name in self.floats:
            r = _KBN.format(pairs=_PAIRS.format(c=c))
            total = f"CASE WHEN count({c}) = 0 THEN NULL ELSE {r}.s + {r}.c END"
            return total if fn == "sum" else f"({total}) / count({c})"
        return f"{fn.upper()}({c})"

    def execute(self, sql: str, params=()) -> Records:
        self.con.execute(sql, [p if p is None or isinstance(p, str) else str(p) for p in params])
        return self

    def fetchone(self):
        return self.con.fetchone()

    def fetchall(self) -> list[tuple]:
        return self.con.fetchall()

    def fetchmany(self, n: int) -> list[tuple]:
        return self.con.fetchmany(n)

    def __iter__(self):
        return iter(self.con.fetchall())

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Records:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect(parquet: Path, names: list[str] | None = None) -> Records:
    return Records(parquet, names)
