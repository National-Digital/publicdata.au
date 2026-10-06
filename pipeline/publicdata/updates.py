"""Rolling sources and feeds: the change log of each fetch, when a fetch becomes a snapshot, and
for a feed, the table of every row state it has held with the first and last fetch that held it.

A release is a dated edition and every changed fetch is a version. A rolling source sends its
whole table each time and a feed sends only what is current, so each fetch is compared with the
one before by key, the newest fetch is served at latest/, and a dated snapshot is cut only by the
rules in `cut`."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import Counter
from dataclasses import replace

import pyarrow as pa

from . import periods
from .diff import diff
from .normalise import Table
from .register import Dataset
from .store import Manifest

# More than this share of the previous fetch's rows added, removed or changed cuts a snapshot.
CHURN = 0.05
# The keys a change log lists of each kind; the counts are always whole.
KEYS = 10_000
FIRST_SEEN = "first_seen"
LAST_SEEN = "last_seen"


def stable(tbl: Table) -> Table:
    """The table without its volatile columns, which never count as a change."""
    drop = [c for c in tbl.dataset.volatile if c in tbl.table.column_names]
    return replace(tbl, table=tbl.table.drop_columns(drop)) if drop else tbl


def digest(tbl: Table) -> str:
    """SHA-256 of the rows without their volatile columns, in sorted order."""
    t = stable(tbl).table
    cols = [t.column(n).to_pylist() for n in t.column_names]
    h = hashlib.sha256(repr(t.schema).encode())
    for r in sorted(hashlib.sha256(repr(r).encode()).digest() for r in zip(*cols, strict=True)):
        h.update(r)
    return h.hexdigest()


def _states(tbl: Table) -> Counter:
    """(period, row digest) for every row, without the volatile columns."""
    ds = tbl.dataset
    t = stable(tbl).table
    labels = periods.labels(tbl.table.column(ds.period.field), ds.period.grain).to_pylist()
    cols = [t.column(n).to_pylist() for n in t.column_names]
    rows = (hashlib.sha256(repr(r).encode()).digest() for r in zip(*cols, strict=True))
    return Counter(zip(labels, rows, strict=True))


def touched(before: Table, after: Table) -> list[str]:
    """The periods whose rows differ between two fetches: a row added, removed or changed
    touches the period of its date, and a row whose date moved touches both."""
    if before.dataset.period is None:
        return []
    a, b = _states(before), _states(after)
    return periods.ordered({p for p, _ in (a - b) + (b - a)})


def compare(before: Table | None, after: Table, day: dt.date) -> dict:
    """The change log of the fetch `after` against the fetch before it, by key, with the volatile
    columns left out. A finished period that changed is a revision."""
    ds = after.dataset
    if before is None:
        log = {
            "dataset": ds.slug,
            "from": None,
            "to": after.manifest.version,
            "rows_from": 0,
            "rows_to": after.rows,
            "key": list(ds.key),
            "added": after.rows,
            "removed": 0,
            "changed": 0,
            "unchanged": 0,
            "note": "The first fetch; every row is new.",
        }
    else:
        log = diff(stable(before), stable(after))
        for k in ("added_keys", "removed_keys", "changed_keys"):
            if len(log.get(k, [])) > KEYS:
                log[k] = log[k][:KEYS]
                log["truncated"] = True
    if ds.volatile:
        log["volatile"] = list(ds.volatile)
    if ds.period is not None:
        log["periods"] = (
            touched(before, after)
            if before is not None
            else periods.ordered(
                set(
                    periods.labels(after.table.column(ds.period.field), ds.period.grain).to_pylist()
                )
            )
        )
        log["revised"] = (
            [p for p in log["periods"] if periods.finished(p, day, ds.period)]
            if before is not None
            else []
        )
    return log


def moved(log: dict) -> int:
    return log.get("added", 0) + log.get("removed", 0) + log.get("changed", 0)


def cut(ds: Dataset, log: dict | None, snapshots: list[Manifest], day: dt.date) -> str:
    """Why this fetch becomes a dated snapshot, or "" when it stays a fetch. `log` is None when
    the rows are those of the fetch before, which is itself no snapshot."""
    if not snapshots:
        return "first"
    if log is not None and log.get("revised"):
        return "revision"
    if log is not None and moved(log) > CHURN * max(log.get("rows_from", 0), 1):
        return "churn"
    last = dt.date.fromisoformat(snapshots[-1].version)
    if ds.period is not None and periods.of_day(day, ds.period.grain) != periods.of_day(
        last, ds.period.grain
    ):
        return "period-close"
    if (day.year, day.month) != (last.year, last.month):
        return "monthly"
    return ""


def closing(per, fetched: dt.date, today: dt.date) -> str:
    """Why a fetch that is no snapshot becomes one when a later read finds it unchanged: it was
    the last change before its period (the one its manifest records) closed, or of its month.
    "" while both are still open."""
    if per is not None and periods.of_day(fetched, per.grain) != periods.of_day(today, per.grain):
        return "period-end"
    if (fetched.year, fetched.month) != (today.year, today.month):
        return "month-end"
    return ""


CUT_WORDS = {
    "first": "it was the first fetch",
    "revision": "a finished period changed",
    "churn": f"more than {CHURN:.0%} of rows changed",
    "period-close": "a period closed",
    "monthly": "it was the first change of the month",
    "month-end": "it was the last change of its month",
    "period-end": "it was the last change before its period closed",
}


def history(seen: pa.Table | None, tbl: Table, day: str, last: str) -> pa.Table:
    """A feed's history after one more fetch: one row per state a key has held, without the
    volatile columns, with the first and last fetch that held it. A state still in the feed at
    the fetch dated `last` is carried to `day`; a state the feed no longer holds keeps its last
    date, and one that comes back starts a new row. States are compared on the columns both
    sides hold, so a column that stops being volatile joins the history as null for old rows."""
    ds = tbl.dataset
    cur = stable(tbl).table
    d = dt.date.fromisoformat(day)
    if seen is None:
        seen = cur.slice(0, 0).append_column(FIRST_SEEN, pa.array([], pa.date32()))
        seen = seen.append_column(LAST_SEEN, pa.array([], pa.date32()))
    shared = [n for n in cur.column_names if n in seen.column_names]

    def keyed(t: pa.Table) -> list[bytes]:
        cols = [t.column(n).to_pylist() for n in shared]
        return [hashlib.sha256(repr(r).encode()).digest() for r in zip(*cols, strict=True)]

    now = keyed(cur)
    held = set(now)
    old = keyed(seen)
    lasts = seen.column(LAST_SEEN).to_pylist()
    was = dt.date.fromisoformat(last) if last else None
    still = [lasts[i] == was and h in held for i, h in enumerate(old)]
    carried = seen.set_column(
        seen.schema.get_field_index(LAST_SEEN),
        LAST_SEEN,
        pa.array([d if s else x for s, x in zip(still, lasts, strict=True)], pa.date32()),
    )
    for name in cur.column_names:
        if name not in carried.column_names:
            carried = carried.append_column(
                cur.schema.field(name), pa.nulls(carried.num_rows, cur.schema.field(name).type)
            )
    kept = {h for s, h in zip(still, old, strict=True) if s}
    fresh = cur.filter(pa.array([h not in kept for h in now], pa.bool_()))
    fresh = fresh.append_column(FIRST_SEEN, pa.array([d] * fresh.num_rows, pa.date32()))
    fresh = fresh.append_column(LAST_SEEN, pa.array([d] * fresh.num_rows, pa.date32()))
    out = pa.concat_tables([carried, fresh], promote_options="permissive")
    extra = [
        n
        for n in seen.column_names
        if n not in cur.column_names and n not in (FIRST_SEEN, LAST_SEEN)
    ]
    out = out.select([*cur.column_names, *extra, FIRST_SEEN, LAST_SEEN])
    order = [(k, "ascending") for k in ds.key] + [(FIRST_SEEN, "ascending")]
    return out.sort_by(order).combine_chunks()


def counts(log: dict) -> dict:
    """A change log's numbers, for the index of fetches."""
    keep = ("from", "to", "rows_from", "rows_to", "added", "removed", "changed", "unchanged")
    out = {k: log[k] for k in keep if k in log}
    for k in ("periods", "revised"):
        if log.get(k):
            out[k] = log[k]
    return out
