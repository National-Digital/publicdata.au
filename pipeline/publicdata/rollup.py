"""Rollups: one version's row counts and totals grouped over the fields its readers filter and
group on, so the MCP server can answer a count from one small object instead of the table.

A rollup holds several groupings ("cubes"): one per askable field, one per pair of them, and
the field sets the register's example and chart ask about, chosen by how many such questions
each answers per byte until the version's cap is spent. A question is answered from the
smallest cube holding every field it names.

A rollup is a cache of answers the query API gives for that version, kept in R2 under
`_rollup/`, outside the published tree. It is never a download and carries the version's
provenance header like every answer does. Rollups are written by the deploy after the build,
so they shape no version's files and the build cache does not key on this module.
"""

from __future__ import annotations

import datetime as dt
import gzip
import itertools
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

FORMAT = "publicdata-rollup/2"
PREFIX = "_rollup/"
# Set from the October 2026 measurement over every live dataset (docs/ARCHITECTURE.md,
# "Rollups").
CAP_BYTES = 1_000_000
MAX_METRICS = 4
# A smaller table is answered at once by any engine.
MIN_ROWS = 5_000
# A field with more values than this is a name or an id, not something readers count by; a
# date is kept whatever its count, since readers ask for periods.
MAX_VALUES = 1_000
# A cube with more groups than this share of the rows is the table again.
MAX_GROUP_SHARE = 0.5
# Pairs are weighed among this many of the most askable fields.
PAIR_FIELDS = 24
# Grouping sets per pass over the table, which bounds the memory one pass takes.
CUBES_PER_PASS = 16
# Weights of the questions a cube answers: the register's example and chart, a count by one
# field, and a count by one field filtered on another.
WEIGHTS = {"hint": 100, "one": 10, "two": 1}
DIM_TYPES = ("string", "integer", "boolean", "date", "datetime", "number")
METRIC_TYPES = ("integer", "number", "boolean")
# Gzipped bytes per group, fitted on the measurement, to rank cubes before they are built.
BYTES_PER_GROUP = 1.2
BYTES_PER_DIM = 0.9
BYTES_PER_METRIC = 6.0

Run = Callable[[str], list[tuple]]


def key(slug: str, version: str) -> str:
    return f"{PREFIX}{slug}/{version}.json.gz"


@dataclass(frozen=True)
class Plan:
    cubes: tuple[tuple[str, ...], ...]
    metrics: tuple[str, ...]
    groups: tuple[int, ...]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def hints(ds) -> list[tuple[str, ...]]:
    """The field sets the register's example and chart ask about."""
    out = []
    if ds.example:
        out.append(
            (*ds.example.get("group", ()), *(w["field"] for w in ds.example.get("filters") or ()))
        )
    if ds.chart and not ds.chart.get("off"):
        out.append(
            (
                *(w["field"] for w in ds.chart.get("where") or ()),
                *(x for x in (ds.chart.get("split"), ds.chart.get("year")) if x),
            )
        )
    return [tuple(dict.fromkeys(h)) for h in out if h]


def distinct_names(ds) -> list[str]:
    return [f.name for f in ds.fields if f.type in DIM_TYPES]


def askable(ds, distinct: dict[str, int], rows: int) -> list[str]:
    """Fields readers count by, the register's hinted ones first, then from fewest values up."""
    types = {f.name: f.type for f in ds.fields}
    ok = [
        f
        for f, n in distinct.items()
        if 2 <= n <= rows * MAX_GROUP_SHARE
        and (n <= MAX_VALUES or types[f] in ("date", "datetime"))
    ]
    hinted = [f for h in hints(ds) for f in h if f in ok]
    rest = sorted((f for f in ok if f not in hinted), key=lambda f: (distinct[f], f))
    return list(dict.fromkeys(hinted + rest))


def _metrics(ds, max_metrics: int) -> list[str]:
    types = {f.name: f.type for f in ds.fields}
    hinted = []
    for m in ((ds.example or {}).get("metric", ""), (ds.chart or {}).get("metric", "")):
        if m and "." in m and types.get(m.split(".", 1)[1]) in METRIC_TYPES:
            hinted.append(m.split(".", 1)[1])
    rest = [n for n, t in types.items() if t in METRIC_TYPES and n not in hinted]
    return list(dict.fromkeys(hinted + rest))[:max_metrics]


def estimate(groups: int, dims: int, metrics: int) -> float:
    return groups * (BYTES_PER_GROUP + BYTES_PER_DIM * dims + BYTES_PER_METRIC * metrics)


def candidates(ds, fields: list[str]) -> dict[tuple[str, ...], dict[frozenset, int]]:
    """Each cube worth weighing, with the questions it would answer and their weights."""
    names = set(distinct_names(ds))
    hinted = [tuple(sorted(h)) for h in hints(ds) if set(h) <= names]
    cubes = {*hinted, *((f,) for f in fields)}
    cubes |= {tuple(sorted(p)) for p in itertools.combinations(fields[:PAIR_FIELDS], 2)}
    out = {}
    for c in cubes:
        qs: dict[frozenset, int] = {}
        for h in hinted:
            if set(h) <= set(c):
                qs[frozenset(h)] = qs.get(frozenset(h), 0) + WEIGHTS["hint"]
        for f in c:
            if f in fields:
                qs[frozenset([f])] = qs.get(frozenset([f]), 0) + WEIGHTS["one"]
        for p in itertools.combinations(c, 2):
            if set(p) <= set(fields[:PAIR_FIELDS]):
                qs[frozenset(p)] = qs.get(frozenset(p), 0) + 2 * WEIGHTS["two"]
        out[c] = qs
    return out


def plan(
    ds,
    run: Run,
    rows: int,
    cap: int = CAP_BYTES,
    max_metrics: int = MAX_METRICS,
    distinct: dict[str, int] | None = None,
) -> Plan | None:
    """The cubes a version's rollup holds within its cap: greedily, the cube that answers the
    most weight of questions not yet answered per estimated byte."""
    if rows < MIN_ROWS:
        return None
    if distinct is None:
        distinct = distinct_counts(run, distinct_names(ds))
    fields = askable(ds, distinct, rows)
    cands = candidates(ds, fields)
    if not cands:
        return None
    sizes = {c: distinct[c[0]] for c in cands if len(c) == 1}
    sizes |= group_counts(run, [c for c in cands if len(c) > 1])
    metrics = _metrics(ds, max_metrics)
    chosen: list[tuple[str, ...]] = []
    answered: set[frozenset] = set()
    spent = 0.0
    left = {c for c in cands if sizes[c] <= rows * MAX_GROUP_SHARE}
    while left:
        best, score = None, 0.0
        for c in left:
            gain = sum(w for q, w in cands[c].items() if q not in answered)
            s = gain / estimate(sizes[c], len(c), len(metrics)) if gain else 0.0
            if s > score or (s == score and s and best is not None and c < best):
                best, score = c, s
        if best is None:
            break
        left.discard(best)
        cost = estimate(sizes[best], len(best), len(metrics))
        if spent + cost > cap:
            continue
        chosen.append(best)
        spent += cost
        answered |= set(cands[best])
    if not chosen:
        return None
    return Plan(tuple(chosen), tuple(metrics), tuple(sizes[c] for c in chosen))


def distinct_counts(run: Run, names: Sequence[str]) -> dict[str, int]:
    if not names:
        return {}
    sel = [
        f"approx_count_distinct({_q(n)}) + MAX(CASE WHEN {_q(n)} IS NULL THEN 1 ELSE 0 END)"
        for n in names
    ]
    row = run("SELECT " + ", ".join(sel) + " FROM records")[0]
    return {n: int(c) for n, c in zip(names, row, strict=True)}


def group_counts(run: Run, cubes: Sequence[tuple[str, ...]]) -> dict[tuple[str, ...], int]:
    """The number of groups in each cube, estimated in one pass; nulls count as a value."""
    if not cubes:
        return {}
    sel = [
        "approx_count_distinct(struct_pack("
        + ", ".join(f"c{i} := {_q(f)}" for i, f in enumerate(c))
        + "))"
        for c in cubes
    ]
    row = run("SELECT " + ", ".join(sel) + " FROM records")[0]
    return {c: max(1, int(n)) for c, n in zip(cubes, row, strict=True)}


def _value(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _order(v):
    return (v is not None, isinstance(v, str), v if v is not None else 0)


def cubes(run: Run, p: Plan, types: dict[str, str]) -> list[dict]:
    """The cubes, a batch of grouping sets per pass over the table so memory stays bounded.
    GROUPING() tells a group's own null from a field the cube does not group on."""
    out = []
    for i in range(0, len(p.cubes), CUBES_PER_PASS):
        out += _pass(run, p.cubes[i : i + CUBES_PER_PASS], p.metrics, types)
    return out


def _pass(run: Run, batch, metrics, types: dict[str, str]) -> list[dict]:
    cols = list(dict.fromkeys(f for c in batch for f in c))
    sets = ", ".join("(" + ", ".join(_q(f) for f in c) + ")" for c in batch)
    sel = [f"GROUPING({', '.join(_q(f) for f in cols)})", *(_q(f) for f in cols), "COUNT(*)"]
    for m in metrics:
        x = f"CAST({_q(m)} AS INTEGER)" if types[m] == "boolean" else _q(m)
        sel += [f"SUM({x})", f"COUNT({_q(m)})", f"MIN({x})", f"MAX({x})"]
    rows = run(f"SELECT {', '.join(sel)} FROM records GROUP BY GROUPING SETS ({sets})")
    n = len(cols)
    by_mask: dict[int, list[tuple]] = {}
    for r in rows:
        by_mask.setdefault(int(r[0]), []).append(tuple(_value(v) for v in r[1:]))
    out = []
    for c in batch:
        mask = sum(1 << (n - 1 - i) for i, f in enumerate(cols) if f not in c)
        ix = [cols.index(f) for f in c]
        mets = [(k, m) for k, m in enumerate(metrics) if m not in c]
        got = sorted(by_mask.get(mask, []), key=lambda r: tuple(_order(r[i]) for i in ix))
        values = [sorted({r[i] for r in got}, key=_order) for i in ix]
        index = [{v: j for j, v in enumerate(vals)} for vals in values]
        out.append(
            {
                "dims": list(c),
                "values": values,
                "codes": [[index[j][r[i]] for r in got] for j, i in enumerate(ix)],
                "count": [r[n] for r in got],
                "metrics": {
                    m: {
                        s: [r[n + 1 + 4 * k + j] for r in got]
                        for j, s in enumerate(("sum", "n", "min", "max"))
                    }
                    for k, m in mets
                },
            }
        )
    return out


def build(ds, run: Run, p: Plan, header: dict, slug: str, version: str) -> bytes:
    """The rollup as gzipped JSON. Each cube lists a field's values once and its groups refer
    to them by position, so a field with long names costs little."""
    obj = {
        "format": FORMAT,
        "slug": slug,
        "version": version,
        "rows": int(run("SELECT COUNT(*) FROM records")[0][0]),
        "fields": [{"name": f.name, "type": f.type} for f in ds.fields],
        "cubes": cubes(run, p, {f.name: f.type for f in ds.fields}),
        "publicdata": header,
    }
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return gzip.compress(body.encode(), compresslevel=9, mtime=0)


def make(ds, run: Run, rows: int, header: dict, slug: str, version: str, cap: int = CAP_BYTES):
    """The planned rollup, built, with its last-chosen cubes dropped until it fits the cap: the
    plan estimates sizes, and decimal totals can run longer than the estimate allows."""
    p = plan(ds, run, rows, cap)
    while p is not None:
        body = build(ds, run, p, header, slug, version)
        if len(body) <= cap:
            return p, body
        keep = min(len(p.cubes) - 1, int(len(p.cubes) * cap / len(body) * 0.95))
        p = Plan(p.cubes[:keep], p.metrics, p.groups[:keep]) if keep > 0 else None
    return None, b""


def parquet_run(path: Path):
    import duckdb

    con = duckdb.connect()
    con.execute(f"CREATE VIEW records AS SELECT * FROM read_parquet('{path.as_posix()}')")
    return (lambda sql: con.execute(sql).fetchall()), con


def parquet_header(path: Path) -> dict:
    import pyarrow.parquet as pq

    meta = pq.read_schema(path).metadata or {}
    return json.loads(meta.get(b"publicdata", b"{}"))


def write(roots: list[Path], datasets, have: set[str], out: Path, log=print) -> list[Path]:
    """A rollup for every built version of each queryable table that R2 does not hold yet."""
    written = []
    for ds in datasets:
        if ds.kind != "table" or not ds.query:
            continue
        seen: set[str] = set()
        for root in roots:
            for src in sorted((root / "d" / ds.slug / "v").glob("*/data.parquet")):
                version = src.parent.name
                k = key(ds.slug, version)
                if version in seen or k in have:
                    continue
                seen.add(version)
                run, con = parquet_run(src)
                try:
                    rows = int(run("SELECT COUNT(*) FROM records")[0][0])
                    p, body = make(ds, run, rows, parquet_header(src), ds.slug, version)
                    if p is None:
                        continue
                finally:
                    con.close()
                dest = out / k
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
                written.append(dest)
                log(
                    f"rollup: {ds.slug}@{version} {len(p.cubes)} cubes, {sum(p.groups)} groups, {len(body)} bytes"
                )
    return written
