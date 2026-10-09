"""Periods: the parts a table with a `period` is split into.

A part is named by the year, the July to June financial year, the quarter or the month of each
row's own date, with an `undated` part for rows that carry none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.compute as pc

from .compute import fill_null
from .register import Period

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Iterable

    from .normalise import Arr, ArrowChunked
    from .store import Manifest, ManifestPeriod

UNDATED = "undated"
# A part's Parquet stays at or under this, and the grain is the largest that keeps it there. The
# same size, or ROWS_MAX rows, is where a dated table must be split at all.
PART_MAX = 100 * 1024 * 1024
ROWS_MAX = 5_000_000


FINER = {"year": "quarter", "fiscal": "quarter", "quarter": "month", "month": ""}
COARSER = {"month": "quarter", "quarter": "year", "year": "", "fiscal": ""}


def _pad(n: Arr) -> Arr:
    return pc.utf8_lpad(pc.cast(n, pa.string()), 4, "0")


def labels(col: Arr, grain: str) -> ArrowChunked:
    """Each row's period as text.

    That is 2025, 2025-26 (July 2025 to June 2026), 2025-Q1 or 2025-01, and `undated` for a null.
    Years are four digits, so labels sort as dates do.
    """
    year = _pad(col) if pa.types.is_integer(col.type) else _pad(pc.year(col))  # type: ignore[arg-type]  # pyarrow-stubs 20 takes only a timestamp; a date column works
    out: Arr
    if grain == "year":
        out = year
    elif grain == "fiscal":
        start = pc.subtract(pc.year(col), pc.cast(pc.less(pc.month(col), 7), pa.int64()))  # type: ignore[arg-type, call-overload]  # pyarrow-stubs 20 takes only a timestamp and no Python scalar
        end = pc.utf8_lpad(pc.cast(pc.add(start, 1), pa.string()), 4, "0")
        out = pc.binary_join_element_wise(_pad(start), pc.utf8_slice_codeunits(end, 2, 4), "-")  # type: ignore[call-overload]  # pyarrow-stubs 20 takes no Python scalar or mixed string arrays
    elif grain == "quarter":
        q = pc.binary_join_element_wise("Q", pc.cast(pc.quarter(col), pa.string()), "")  # type: ignore[call-overload]  # pyarrow-stubs 20 takes no Python scalar or mixed string arrays
        out = pc.binary_join_element_wise(year, q, "-")
    else:
        m = pc.utf8_lpad(pc.cast(pc.month(col), pa.string()), 2, "0")
        out = pc.binary_join_element_wise(year, m, "-")  # type: ignore[call-overload]  # pyarrow-stubs 20 takes no Python scalar or mixed string arrays
    out = fill_null(out, UNDATED)
    return out if isinstance(out, pa.ChunkedArray) else pa.chunked_array([out])


def recorded(ds: Period | None) -> ManifestPeriod | None:
    """A period as a fetch records it in its manifest."""
    if ds is None:
        return None
    return {"field": ds.field, "grain": ds.grain, "revision_window": ds.revision_window}


def of_manifest(m: Manifest) -> Period | None:
    """The period a version is split by: the one its manifest recorded when it was fetched."""
    p = getattr(m, "period", None)
    return Period(p["field"], p["grain"], p.get("revision_window", 2)) if p else None


def of_day(day: dt.date, grain: str) -> str:
    if grain == "year":
        return f"{day.year:04d}"
    if grain == "fiscal":
        y = day.year if day.month >= 7 else day.year - 1  # noqa: PLR2004 - July starts it
        return f"{y:04d}-{(y + 1) % 100:02d}"
    if grain == "quarter":
        return f"{day.year:04d}-Q{(day.month - 1) // 3 + 1}"
    return f"{day.year:04d}-{day.month:02d}"


def _step_back(label: str, grain: str) -> str:
    if grain == "year":
        return f"{int(label) - 1:04d}"
    if grain == "fiscal":
        start = int(label[:4]) - 1
        return f"{start:04d}-{(start + 1) % 100:02d}"
    y, _, rest = label.partition("-")
    n = int(rest.lstrip("Q"))
    if n > 1:
        return f"{y}-Q{n - 1}" if grain == "quarter" else f"{y}-{n - 1:02d}"
    return f"{int(y) - 1:04d}-Q4" if grain == "quarter" else f"{int(y) - 1:04d}-12"


def open_since(day: dt.date, period: Period) -> str:
    """The oldest open period on a day: the current one and the revision_window - 1 before it."""
    label = of_day(day, period.grain)
    for _ in range(period.revision_window - 1):
        label = _step_back(label, period.grain)
    return label


def finished(label: str, day: dt.date, period: Period) -> bool:
    """Whether a part is closed on a day.

    Undated rows are never finished, since any fetch can add to them.
    """
    return label != UNDATED and label < open_since(day, period)


def ordered(names: Iterable[str]) -> list[str]:
    return sorted(names, key=lambda n: (n == UNDATED, n))


def coarser(grain: str) -> str:
    return COARSER[grain]


def regrain(label: str, grain: str) -> str:
    """A label read at a coarser grain: 2025-03 as 2025-Q1 or 2025."""
    if label == UNDATED or grain == "month":
        return label
    y, _, rest = label.partition("-")
    if grain == "year" or not rest:
        return y
    if rest.startswith("Q"):
        return label
    return f"{y}-Q{(int(rest) - 1) // 3 + 1}"


def grain_problem(sizes: dict[str, int], grain: str, limit: int | None = None) -> str:
    """Why the grain is wrong for parts of these Parquet sizes, or "".

    A part over the limit needs a shorter period; when the next longer period would still keep
    every part under it, the grain is shorter than it needs to be. Summed sizes stand in for the
    merged files, which compress at least as well.
    """
    limit = PART_MAX if limit is None else limit
    big = [k for k, n in sizes.items() if n > limit]
    if big:
        finer = FINER[grain]
        return f"part {big[0]} is {sizes[big[0]]} bytes, over {limit}" + (
            f"; use grain {finer}" if finer else ""
        )
    up = coarser(grain)
    if up:
        merged: dict[str, int] = {}
        for k, n in sizes.items():
            merged[regrain(k, up)] = merged.get(regrain(k, up), 0) + n
        if max(merged.values(), default=0) <= limit:
            return f"grain {up} keeps every part under {limit} bytes; use it"
    return ""
