"""A table with a `period` is written as one part per period, each part its own build unit.

A part's rows are digested on their own, and a finished part whose rows are those of the snapshot
before is not written again; the new snapshot's manifest points at the earlier file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pyarrow as pa
import pyarrow.compute as pc

from . import SITE, periods
from .serialise import duckdb_connect, duckdb_meta, field_rows, profile
from .serialise.writers.csv_gz import write_csv_gz
from .serialise.writers.parquet import write_parquet

if TYPE_CHECKING:
    from .build import HeaderFor, PartFile, PartRecord
    from .normalise import Table
    from .provenance import Header
    from .register import Period
    from .serialise.profile import Layout

FORMATS: tuple[str, ...] = ("parquet", "csv.gz")
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


def url(slug: str, rec: PartRecord, fmt: str) -> str:
    """Where a part's file is published.

    That is under the tree that wrote it, which for a finished part may be an earlier snapshot's.
    """
    tree = f"d/{slug}/latest" if rec["tree"] == "latest" else f"d/{slug}/v/{rec['tree']}"
    return f"{SITE}/{tree}/{rec['files'][fmt]['path']}"


def _layout(t: pa.Table, lay: Layout) -> Layout:
    """The version's recorded Parquet layout as it applies to table t.

    A feed's history lacks the volatile columns, so a sort, lookup or INT32 field it does not hold
    is left out.
    """
    if not lay:
        return {}
    have = set(t.column_names)
    # The keys are the layout's own, and every key after profile holds a list of field names.
    return cast(
        "Layout",
        {
            k: [c for c in cast("list[str]", v) if c in have] if k in profile.LAYOUT_KEYS[1:] else v
            for k, v in lay.items()
        },
    )


def _schema(sub: pa.Table, lay: Layout) -> str:
    """The part's Parquet column types as the layout writes them, for the reuse test."""
    empty = sub.slice(0, 0)
    if lay.get("int32"):
        empty = profile.narrow(empty, lay["int32"])
    return hashlib.sha256(str(empty.schema.remove_metadata()).encode()).hexdigest()


def write(  # noqa: PLR0913, PLR0917 - build passes them in place
    tbl: Table,
    t: pa.Table,
    per: Period,
    hdr: HeaderFor,
    vdir: Path,
    tree: str,
    prior: list[PartRecord],
    name: str = "parts",
    revised: set[str] | None = None,
) -> tuple[list[PartRecord], list[str]]:
    """The parts of table t under vdir/name/, one per period of `per`, and the periods revised.

    `prior` is the snapshot before's record of its parts: a finished part whose rows, volatile
    columns included, and column types are the same is taken from it unwritten. `revised` is the
    set the change logs since that snapshot name; without one (a release), a finished part whose
    rows outside the volatile columns differ from the snapshot before's is a revision.
    """
    day = dt.date.fromisoformat(tbl.manifest.version)
    labels = periods.labels(t.column(per.field), per.grain)
    before = {r["period"]: r for r in prior}
    names = periods.ordered(set(cast("list[str]", labels.to_pylist())))
    out: list[PartRecord] = []
    volatile = [c for c in tbl.manifest.volatile if c in t.column_names]
    # Every part follows the layout the version's manifest records, so all have the same types.
    lay = _layout(t, tbl.manifest.parquet)
    flagged = set(revised or ())
    for p in names:
        sub = t.filter(pc.equal(labels, p))  # type: ignore[call-overload]  # pyarrow-stubs 20 takes no Python scalar
        digest = rows_digest(sub)
        steady = rows_digest(sub.drop_columns(volatile)) if volatile else digest
        schema = _schema(sub, lay)
        done = periods.finished(p, day, per)
        was = before.get(p)
        if (
            revised is None
            and done
            and was
            and was["finished"]
            and was.get("stable_sha256", was["rows_sha256"]) != steady
        ):
            flagged.add(p)
        same = (
            was is not None
            and was["finished"]
            and was["rows_sha256"] == digest
            and was.get("schema") == schema
        )
        if done and same and was is not None:
            out.append({**was, "revised": False})
            continue
        files: dict[str, PartFile] = {}
        d = vdir / name
        d.mkdir(parents=True, exist_ok=True)
        part = replace(tbl, table=sub, geometry=None, order=None)
        for fmt in FORMATS:
            rel = f"{name}/{p}.{fmt}"
            h = hdr(sub.num_rows, rel)
            h["period"] = {"field": per.field, "grain": per.grain, "value": p}  # type: ignore[typeddict-unknown-key]  # provenance.Header names no period yet
            if fmt == "parquet":
                write_parquet(part, h, vdir / rel, lay=lay)
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    write_csv_gz(part, vdir / rel, Path(tmp))
                    # The query API loads a version stored as parts while their CSV is as small
                    # as a whole data.csv it loads (d1.MAX_CSV).
                    csv = (Path(tmp) / "data.csv").stat().st_size
            files[fmt] = {"path": rel, "bytes": (vdir / rel).stat().st_size}
            if fmt == "csv.gz":
                files[fmt]["csv_bytes"] = csv
        out.append(
            {
                "period": p,
                "rows": sub.num_rows,
                "rows_sha256": digest,
                **({"stable_sha256": steady} if volatile else {}),  # type: ignore[typeddict-item]  # mypy reads the optional key as a whole record
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


def whole(records: list[PartRecord], rows: int) -> bool:
    """Whether the whole-table files are written beside the parts."""
    size = sum(r["files"]["parquet"]["bytes"] for r in records)
    return rows <= WHOLE_ROWS and size <= WHOLE_BYTES


def write_duckdb(tbl: Table, records: list[PartRecord], header: Header, path: Path) -> None:
    """A DuckDB file for a version too large to be one file.

    It holds a `parts` table naming each part with its URL, and a records() table macro over every
    part's Parquet, which reads them over HTTPS when it is called. records(files := [...]) reads a
    chosen few.
    """
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
