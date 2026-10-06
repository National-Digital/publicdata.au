"""A table with a `period` is written as one part per period, each part its own build unit: a
part's rows are digested on their own, and a finished part whose rows are those of the snapshot
before is not written again; the new snapshot's manifest points at the earlier file."""

from __future__ import annotations

import datetime as dt
import hashlib
import tempfile
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from . import SITE, periods
from .normalise import Table
from .serialise.writers.csv_gz import write_csv_gz
from .serialise.writers.parquet import write_parquet

try:
    from .serialise import profile
except ImportError:  # the Parquet profile lands separately; until then every part is as written
    profile = None

FORMATS = ("parquet", "csv.gz")
# The whole-table files are written beside the parts while the table is this small; above it a
# version is its parts and a DuckDB file whose records() reads them over HTTPS.
WHOLE_BYTES = periods.PART_MAX
WHOLE_ROWS = periods.ROWS_MAX


def rows_digest(t: pa.Table) -> str:
    """SHA-256 of a part's rows in sorted order, so a reordered export is the same part."""
    cols = [t.column(n).to_pylist() for n in t.column_names]
    h = hashlib.sha256(repr(t.schema.remove_metadata()).encode())
    for r in sorted(hashlib.sha256(repr(r).encode()).digest() for r in zip(*cols, strict=True)):
        h.update(r)
    return h.hexdigest()


def url(slug: str, rec: dict, fmt: str) -> str:
    """Where a part's file is published: under the tree that wrote it, which for a finished part
    may be an earlier snapshot's."""
    tree = f"d/{slug}/latest" if rec["tree"] == "latest" else f"d/{slug}/v/{rec['tree']}"
    return f"{SITE}/{tree}/{rec['files'][fmt]['path']}"


def _schema(sub: pa.Table, narrow: dict) -> str:
    """The part's Parquet column types as they would be written, for the reuse test."""
    empty = sub.slice(0, 0)
    if profile and narrow:
        empty = profile.narrow(empty, narrow["int32"])
    return hashlib.sha256(str(empty.schema.remove_metadata()).encode()).hexdigest()


def write(
    tbl: Table,
    t: pa.Table,
    per,
    hdr,
    vdir: Path,
    tree: str,
    prior: list[dict],
    name: str = "parts",
    revised: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """The parts of table t under vdir/name/, one per period of `per`, and the periods revised.
    `prior` is the snapshot before's record of its parts: a finished part whose rows, volatile
    columns included, and column types are the same is taken from it unwritten. `revised` is the
    set the change logs since that snapshot name; without one (a release), a finished part whose
    rows outside the volatile columns differ from the snapshot before's is a revision."""
    ds = tbl.dataset
    day = dt.date.fromisoformat(tbl.manifest.version)
    labels = periods.labels(t.column(per.field), per.grain)
    before = {r["period"]: r for r in prior}
    names = periods.ordered(set(labels.to_pylist()))
    out = []
    volatile = [c for c in ds.volatile if c in t.column_names]
    # Narrowed to INT32 by the whole table, so every part of a version has the same column types.
    narrow = {"int32": profile.int32_columns(t)} if profile else {}
    flagged = set(revised or ())
    for p in names:
        sub = t.filter(pc.equal(labels, p))
        digest = rows_digest(sub)
        steady = rows_digest(sub.drop_columns(volatile)) if volatile else digest
        schema = _schema(sub, narrow)
        done = periods.finished(p, day, per)
        was = before.get(p)
        if revised is None and done and was and was["finished"]:
            if was.get("stable_sha256", was["rows_sha256"]) != steady:
                flagged.add(p)
        same = (
            was is not None
            and was["finished"]
            and was["rows_sha256"] == digest
            and was.get("schema") == schema
        )
        if done and same:
            out.append({**was, "revised": False})
            continue
        files = {}
        d = vdir / name
        d.mkdir(parents=True, exist_ok=True)
        part = replace(tbl, table=sub, geometry=None)
        for fmt in FORMATS:
            rel = f"{name}/{p}.{fmt}"
            h = hdr(sub.num_rows, rel)
            h["period"] = {"field": per.field, "grain": per.grain, "value": p}
            if fmt == "parquet":
                write_parquet(part, h, vdir / rel, **narrow)
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    write_csv_gz(part, vdir / rel, Path(tmp))
            files[fmt] = {"path": rel, "bytes": (vdir / rel).stat().st_size}
        out.append(
            {
                "period": p,
                "rows": sub.num_rows,
                "rows_sha256": digest,
                **({"stable_sha256": steady} if volatile else {}),
                "schema": schema,
                "tree": tree,
                "finished": done,
                "revised": p in flagged,
                "files": files,
            }
        )
    if revised is None:
        flagged |= {p for p, r in before.items() if r["finished"] and p not in names}
    return out, periods.ordered(flagged)


def whole(records: list[dict], rows: int) -> bool:
    """Whether the whole-table files are written beside the parts."""
    size = sum(r["files"]["parquet"]["bytes"] for r in records)
    return rows <= WHOLE_ROWS and size <= WHOLE_BYTES


def write_duckdb(tbl: Table, records: list[dict], header: dict, path: Path) -> None:
    """A DuckDB file for a version too large to be one file: a `parts` table naming each part
    with its URL, and a records() table macro over every part's Parquet, which reads them over
    HTTPS when it is called. records(files := [...]) reads a chosen few."""
    from .serialise import duckdb_connect, duckdb_meta, field_rows

    slug = tbl.dataset.slug
    urls = [url(slug, r, "parquet") for r in records]
    con = duckdb_connect(path, len(records))
    try:
        con.execute(
            "CREATE TABLE parts (period VARCHAR, rows BIGINT, url VARCHAR, finished BOOLEAN)"
        )
        con.executemany(
            "INSERT INTO parts VALUES (?, ?, ?, ?)",
            [
                (r["period"], r["rows"], u, r["finished"])
                for r, u in zip(records, urls, strict=True)
            ],
        )
        listed = ", ".join("'" + u.replace("'", "''") + "'" for u in urls)
        con.execute(
            f"CREATE MACRO records(files := [{listed}]::VARCHAR[]) AS TABLE "
            "SELECT * FROM read_parquet(files)"
        )
        duckdb_meta(con, header, field_rows(tbl))
        con.execute("CHECKPOINT")
    finally:
        con.close()
