"""Projected R2 storage growth per register entry, and the gate on entries a pull request changes.

Kept out of build.py's imports so it never enters the build cache key."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from . import SITE, store
from .cadence import FEED_MAX, WEEKLY_MAX, per_year
from .register import Dataset

GB = 10**9
BUDGET_GB_YEAR = 5.0
# Measured across the fleet: published bytes are about 13 times the source bytes.
SOURCE_MULTIPLIER = 13
# The raw bucket and the version's dist tree each hold the publisher's file. Drop to 1 once
# the dist copy is served from raw (#48).
SOURCE_COPIES = 2
DEFAULT_PER_YEAR = 52
R2_USD_PER_GB_MONTH = 0.015
R2_FREE_GB = 10
APPROVAL_LABEL = "cost-approved"
CATALOG = f"{SITE}/catalog.json"
UA = "publicdata.au cost (+https://publicdata.au/about/)"


@dataclass(frozen=True)
class Projection:
    slug: str
    bytes_per_version: int | None
    basis: str  # measured, estimate or unknown
    per_year: float
    per_year_basis: str
    stored_versions: int
    budget_gb: float = BUDGET_GB_YEAR
    # A change to the entry's output rebuilds every stored version once with the new shape.
    rebuild_bytes: int = 0

    @property
    def gb_per_version(self) -> float | None:
        return None if self.bytes_per_version is None else self.bytes_per_version / GB

    @property
    def gb_per_year(self) -> float | None:
        g = self.gb_per_version
        return None if g is None else g * self.per_year + self.rebuild_bytes / GB

    @property
    def over_budget(self) -> bool:
        g = self.gb_per_year
        # An entry whose size cannot be worked out fails closed until a person approves it.
        return (g is None and self.per_year > 0) or (g or 0) > self.budget_gb


def versions_per_year(ds: Dataset, versions: list[str], today: dt.date) -> tuple[float, str]:
    """The larger of the declared rate and the versions of the last year, capped at what the
    fetch can make. The fetch never reads the cadence, so the text alone cannot lower the rate."""
    if not ds.publishable:
        return 0.0, "not publishable"
    cap = FEED_MAX if ds.source.feed else WEEKLY_MAX
    declared = per_year(ds.source.cadence, ds.source.feed, today)
    if not versions:
        n, basis = (declared, "cadence") if declared is not None else (DEFAULT_PER_YEAR, "default")
        # The first fetch stores a version whatever the cadence says.
        return float(min(max(n, 1), cap)), basis
    since = today - dt.timedelta(days=365)
    recent = sum(1 for v in versions if since < dt.date.fromisoformat(v) <= today)
    # An ended cadence cannot bring the rate to zero while the fetch still runs.
    if declared and declared >= recent:
        return float(min(declared, cap)), "cadence"
    # A served entry that has not changed for a year is still expected to change again.
    return float(min(max(recent, 1), cap)), "observed"


def _upper(path: str, n: int) -> int:
    """A DuckDB file's size is published to one significant figure; count its upper bound."""
    if path.endswith(".duckdb") and n >= 10:
        return n + 10 ** (len(str(n)) - 1) // 2
    return n


def catalogue_sizes(catalog: dict) -> dict[str, dict[str, int]]:
    """Bytes of each file of the newest version, by its path inside the version, per slug."""
    out = {}
    for rec in catalog.get("dataset", []):
        mark = f"/v/{rec.get('versionInfo', '')}/"
        files = {}
        for d in rec.get("distribution", []):
            url = d.get("downloadURL", "")
            if mark in url:
                path = url.split(mark, 1)[1]
                files[path] = _upper(path, int(d.get("byteSize") or 0))
        out[rec["identifier"]] = files
    return out


def measured_bytes(ds: Dataset, files: dict[str, int], source_bytes: int) -> int:
    """A version's bytes in R2: its data files, the publisher's file in each bucket that holds
    it, and the partition files, which hold the table again once per partition field. NDJSON
    stands in for the partitions' JSON because data.json is not written for a large table."""
    total = sum(files.values())
    if not ds.source_withheld:
        total += source_bytes * SOURCE_COPIES
    rows_json = files.get("data.ndjson") or files.get("data.json", 0)
    per_part = rows_json
    if ds.geometry and (ds.geometry or {}).get("kind", "point") == "point":
        per_part += files.get("data.geojson") or rows_json
    return total + per_part * len(ds.partition_by)


def _head(url: str, timeout: float) -> int | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if "html" in r.headers.get("Content-Type", ""):
            return None
        n = r.headers.get("Content-Length")
        return int(n) if n else None


def _ranged(url: str, timeout: float) -> int | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = r.headers.get("Content-Range", "").rpartition("/")[2]
        return int(total) if total.isdigit() else None


def _size(url: str, timeout: float) -> int | None:
    """A presigned S3 redirect refuses HEAD, so a one-byte GET reads the size from Content-Range."""
    try:
        if n := _head(url, timeout):
            return n
    except OSError:
        pass
    return _ranged(url, timeout)


def probe(ds: Dataset, timeout: float = 30) -> int | None:
    """The size of the file the fetch would read: the CKAN resource the entry resolves to, the
    way the fetch picks it, or a fixed file URL. Other adapters are not sized."""
    try:
        if ds.source.adapter == "ckan-resource":
            import requests

            from .fetch import _package, pick_resource

            s = requests.Session()
            s.headers["User-Agent"] = UA
            res = pick_resource(ds, _package(ds, s, f"{ds.source.portal.rstrip('/')}/api/3/action")["resources"])  # fmt: skip
            # Some portals give the size as text ("2 MiB"); the host's headers are exact.
            if str(res.get("size") or "").isdigit():
                return int(res["size"])
            return _size(res["url"], timeout)
        if ds.source.adapter == "file" and not ds.source.page_size:
            return _size(ds.source.url, timeout)
    except Exception:  # noqa: BLE001
        return None
    return None


def project(
    datasets: list[Dataset],
    store_dir: Path,
    sizes: dict[str, dict[str, int]] | None,
    today: dt.date,
    changed: frozenset[str] = frozenset(),
    fresh: frozenset[str] = frozenset(),
    prober=probe,
    probing: bool = False,
    reshaped: dict[str, dict] | None = None,
) -> list[Projection]:
    """`sizes` is None when the catalogue could not be read; a changed entry is then unknown.
    `fresh` are changed entries whose source or output shape moved: sized from the new file,
    and never below what the newest version measures. `reshaped` holds the base copy of each
    entry whose output shape changed, so the rebuild of its stored versions is counted."""
    reshaped = reshaped or {}
    out = []
    for ds in datasets:
        ms = store.manifests(store_dir, ds.slug)
        n, n_basis = versions_per_year(ds, [m.version for m in ms], today)
        newest = ms[-1] if ms else None
        b, basis = None, "unknown"
        measured = None
        if sizes and sizes.get(ds.slug):
            measured = measured_bytes(ds, sizes[ds.slug], newest.bytes if newest else 0)
        if sizes is None and ds.slug in changed and ms:
            pass
        elif ds.slug in fresh or (ds.slug in changed and not ms):
            if probing and (src := prober(ds)):
                b, basis = src * SOURCE_MULTIPLIER, "estimate"
                if measured is not None and measured >= b:
                    b, basis = measured, "measured"
        elif measured is not None:
            b, basis = measured, "measured"
        elif newest is not None:
            b, basis = newest.bytes * SOURCE_MULTIPLIER, "estimate"
        rebuild = 0
        if ds.slug in reshaped and ms and b is not None:
            old = reshaped[ds.slug]
            before = replace(
                ds,
                partition_by=tuple(old.get("partition_by") or ()),
                geometry=old.get("geometry") or None,
            )
            was = (
                measured_bytes(before, sizes[ds.slug], newest.bytes)
                if measured is not None
                else newest.bytes * SOURCE_MULTIPLIER
            )
            rebuild = max(0, b - was) * len(ms)
        out.append(Projection(ds.slug, b, basis, n, n_basis, len(ms), rebuild_bytes=rebuild))
    return out


def r2_usd_per_month(gb: float) -> float:
    return max(0.0, gb - R2_FREE_GB) * R2_USD_PER_GB_MONTH


@dataclass(frozen=True)
class Fleet:
    stored_bytes: int
    bytes_per_year: int

    @property
    def stored_gb(self) -> float:
        return self.stored_bytes / GB

    @property
    def gb_per_year(self) -> float:
        return self.bytes_per_year / GB

    @property
    def usd_per_month_now(self) -> float:
        return r2_usd_per_month(self.stored_gb)

    @property
    def usd_per_month_in_a_year(self) -> float:
        return r2_usd_per_month(self.stored_gb + self.gb_per_year)

    def as_json(self) -> dict:
        return {
            "stored_bytes": self.stored_bytes,
            "growth_bytes_per_year": self.bytes_per_year,
            "r2_usd_per_month": round(self.usd_per_month_now, 2),
            "r2_usd_per_month_in_a_year": round(self.usd_per_month_in_a_year, 2),
        }


def fleet(projections: list[Projection]) -> Fleet:
    """Stored bytes count every stored version at its newest version's size, an estimate."""
    return Fleet(
        sum((p.bytes_per_version or 0) * p.stored_versions for p in projections),
        round(sum((p.bytes_per_version or 0) * p.per_year + p.rebuild_bytes for p in projections)),
    )


def build_version_bytes(ds: Dataset, v) -> int:
    """A built version's bytes in R2: every file in its tree, and the raw bucket's copies of
    the publisher's file beyond the one in the tree."""
    extra = 0 if ds.source_withheld else v.manifest.bytes * (SOURCE_COPIES - 1)
    return sum(v.files.values()) + extra


def fleet_from_build(outs, today: dt.date) -> Fleet:
    """The same totals from a build, where every version's file sizes are known."""
    stored = growth = 0
    for o in outs:
        if not o.versions:
            continue
        stored += sum(build_version_bytes(o.dataset, v) for v in o.versions)
        n, _ = versions_per_year(o.dataset, [v.manifest.version for v in o.versions], today)
        growth += build_version_bytes(o.dataset, o.latest) * n
    return Fleet(stored, round(growth))


def _git(root: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def changed_paths(base: str, root: Path) -> tuple[str, list[str]]:
    mb = _git(root, "merge-base", base, "HEAD")
    return mb, _git(root, "diff", "--name-only", mb, "HEAD", "--", "register").splitlines()


def changed_entries(register_dir: Path, paths: list[str], root: Path) -> dict[str, str]:
    """Slug to path for register entries among changed paths; publishers, licences and deleted
    files are not entries."""
    out = {}
    for p in paths:
        full = (root / p).resolve()
        try:
            rel = full.relative_to(register_dir.resolve())
        except ValueError:
            continue
        if (
            full.suffix == ".yaml"
            and full.exists()
            and rel.parts[0] not in ("publishers", "licences")
        ):
            out[full.stem] = p
    return out


# Source keys that change how a file is read, never which file or how much of it.
QUIET_SOURCE_KEYS = ("cadence", "encoding", "as_at_regex")
# Top-level keys that change what a version publishes. partition_by is measured from the
# catalogue's files, so it only changes the rebuild of stored versions.
SHAPE_KEYS = ("unpivot", "wide", "enrich", "geometry", "kind", "database", "tables")


def _source(raw: dict) -> dict:
    src = dict(raw.get("source") or {})
    for k in QUIET_SOURCE_KEYS:
        src.pop(k, None)
    # A CKAN entry's url is the landing page; the fetch reads the portal, package and resource.
    if src.get("adapter") == "ckan-resource":
        src.pop("url", None)
    return src


def _shape(raw: dict) -> dict:
    out = {k: raw.get(k) for k in SHAPE_KEYS}
    # A field's description or label leaves the bytes alone; its name, source and type do not.
    out["fields"] = [
        (f.get("name"), f.get("source"), f.get("type")) if isinstance(f, dict) else f
        for f in raw.get("fields") or ()
    ]
    return out


def _base_paths(root: Path, base: str) -> dict[str, str]:
    """Entry paths in the base by slug, so a file moved into a folder still finds its copy."""
    out = {}
    for p in _git(root, "ls-tree", "-r", "--name-only", base, "--", "register").splitlines():
        parts = Path(p).parts
        if p.endswith(".yaml") and len(parts) > 1 and parts[1] not in ("publishers", "licences"):
            out[Path(p).stem] = p
    return out


def entry_changes(
    root: Path, base: str, entries: dict[str, str]
) -> tuple[set[str], dict[str, dict]]:
    """Changed entries whose source or output shape differs from the base's, so the stored
    file no longer says how big a version will be, and the base copy of each entry whose
    output shape changed. An entry with no base copy counts as moved."""
    fresh, reshaped = set(), {}
    base_paths = _base_paths(root, base)
    for slug, path in entries.items():
        new = yaml.safe_load((root / path).read_text(encoding="utf-8")) or {}
        if slug not in base_paths:
            fresh.add(slug)
            continue
        old = yaml.safe_load(_git(root, "show", f"{base}:{base_paths[slug]}")) or {}
        shaped = _shape(old) != _shape(new)
        if shaped or _source(old) != _source(new):
            fresh.add(slug)
        if shaped or old.get("partition_by") != new.get("partition_by"):
            reshaped[slug] = old
    return fresh, reshaped


def load_catalog(where: str) -> dict:
    if where.startswith(("http://", "https://")):
        req = urllib.request.Request(where, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    return json.loads(Path(where).read_text(encoding="utf-8"))


def _gb(x: float | None) -> str:
    return "?" if x is None else f"{x:.3f}"


BASIS = {"measured": "", "estimate": " (estimate)", "unknown": " (size unknown)"}


def _row(p: Projection) -> str:
    return (
        f"| `{p.slug}` | {_gb(p.gb_per_version)}{BASIS[p.basis]} | {p.per_year:g} ({p.per_year_basis}) "
        f"| {_gb(p.rebuild_bytes / GB)} | {_gb(p.gb_per_year)} | {'yes' if p.over_budget else 'no'} |"
    )


HEAD = (
    "| slug | GB/version | versions/yr | rebuild GB | GB/yr | over budget |",
    "|---|---|---|---|---|---|",
)


def report(
    projections: list[Projection], gated: set[str], approved: bool, note: str = ""
) -> tuple[str, list[Projection]]:
    """The Markdown summary and the gated entries over budget."""
    f = fleet(projections)
    over = [p for p in projections if p.slug in gated and p.over_budget]
    lines = ["## Storage cost", ""]
    if note:
        lines += [note, ""]
    if gated:
        lines += [
            f"Entries this change adds or edits, against {BUDGET_GB_YEAR:g} GB a year each:",
            "",
        ]
        lines += [*HEAD, *(_row(p) for p in projections if p.slug in gated), ""]
        if over and approved:
            lines.append(
                f"Over budget and approved with the `{APPROVAL_LABEL}` label for this commit."
            )
        elif over:
            lines.append(
                f"Over budget: {', '.join(p.slug for p in over)}. A maintainer who accepts the "
                f"cost adds the `{APPROVAL_LABEL}` label, which reruns this check. The label "
                "approves only the commit it was added on, so each new commit is approved again."
            )
        else:
            lines.append("Every changed entry is within budget.")
    else:
        lines.append("This change adds or edits no register entry, so nothing is gated.")
    lines += [
        "",
        "### Fleet",
        "",
        f"Projected growth: {f.gb_per_year:,.1f} GB a year across {len(projections)} entries.",
        f"Stored now (estimated as each stored version at its newest version's size): "
        f"{f.stored_gb:,.1f} GB.",
        f"R2 Standard at ${R2_USD_PER_GB_MONTH} per GB-month after {R2_FREE_GB} GB free: "
        f"${f.usd_per_month_now:,.2f} a month now and ${f.usd_per_month_in_a_year:,.2f} a month "
        "in a year, since storage accumulates.",
        "",
        "A change to an entry's output rebuilds each of its stored versions once, counted at the "
        "newest version's size in the rebuild column and in the year's figure. "
        "Sizes marked estimate are the source bytes times "
        f"{SOURCE_MULTIPLIER}, the fleet's measured ratio; the rest are read from the newest "
        f"version's files in the catalogue, with the publisher's file counted {SOURCE_COPIES} "
        "times for the buckets that hold it.",
        "",
        "<details><summary>Every entry</summary>",
        "",
        *HEAD,
        *(_row(p) for p in sorted(projections, key=lambda p: -(p.gb_per_year or 0))),
        "",
        "</details>",
        "",
    ]
    return "\n".join(lines), over


def run(
    datasets: list[Dataset],
    store_dir: Path,
    catalog_src: str,
    changed: set[str],
    today: dt.date,
    approved: bool = False,
    probing: bool = False,
    fresh: set[str] = frozenset(),
    summary: str | None = None,
    reshaped: dict[str, dict] | None = None,
) -> int:
    known = {d.slug for d in datasets}
    if missing := sorted(changed - known):
        print(f"cost: no register entry named {', '.join(missing)}")
        return 2
    note = ""
    try:
        sizes = catalogue_sizes(load_catalog(catalog_src))
    except (OSError, ValueError) as e:
        sizes = None
        note = (
            f"The catalogue could not be read ({e}). Changed entries count as size unknown and "
            "the rest are estimates."
        )
    projections = project(
        datasets,
        store_dir,
        sizes,
        today,
        frozenset(changed),
        frozenset(fresh),
        probing=probing,
        reshaped=reshaped,
    )
    text, over = report(projections, changed, approved, note)
    f = fleet(projections)
    print(
        f"cost: {f.gb_per_year:,.1f} GB a year projected over {len(projections)} entries; "
        f"{f.stored_gb:,.1f} GB stored (estimated); R2 ${f.usd_per_month_now:,.2f}/month now, "
        f"${f.usd_per_month_in_a_year:,.2f}/month in a year"
    )
    for p in projections:
        if p.slug in changed:
            print(
                f"cost: {p.slug} {_gb(p.gb_per_version)} GB/version ({p.basis}) x "
                f"{p.per_year:g}/yr ({p.per_year_basis}) + {_gb(p.rebuild_bytes / GB)} GB rebuild "
                f"= {_gb(p.gb_per_year)} GB/yr" + (" OVER BUDGET" if p.over_budget else "")
            )
    path = summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    if over and not approved:
        print(f"cost: over budget; add the `{APPROVAL_LABEL}` label to accept the cost")
        return 1
    return 0
