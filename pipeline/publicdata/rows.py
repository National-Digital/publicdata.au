"""A query's answer row, apart from records.py so the build's code does not take in the reader."""

from __future__ import annotations

from typing import Any, Protocol

# A row of a query's answer. DuckDB types its rows' values as Any, and each caller knows from its
# own SQL what a column holds, so a row is named once here.
type Row = tuple[Any, ...]  # type: ignore[explicit-any]  # duckdb types a fetched row's values as Any


class _Cursor(Protocol):
    def fetchone(self) -> Row | None: ...


def one_row(cur: _Cursor) -> Row:
    """The row an aggregate query answers, which it always does."""
    row = cur.fetchone()
    if row is None:
        msg = "an aggregate query answered no row"
        raise RuntimeError(msg)
    return row
