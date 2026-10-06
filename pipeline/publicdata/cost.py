"""Projected R2 storage growth per register entry, and the gate on entries a pull request changes.

Kept out of build.py's imports so it never enters the build cache key."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import SITE, store
from .register import Dataset

GB = 10**9
BUDGET_GB_YEAR = 5.0
# Measured across the fleet: published bytes are about 13 times the source bytes.
SOURCE_MULTIPLIER = 13
DEFAULT_PER_YEAR = 52
R2_USD_PER_GB_MONTH = 0.015
R2_FREE_GB = 10
APPROVAL_LABEL = "cost-approved"
CATALOG = f"{SITE}/catalog.json"
UA = "publicdata.au cost (+https://publicdata.au/about/)"

# First match wins, so the narrower phrases come first.
CADENCE = (
    (r"checked weekly", 52),
    (r"\buntil\b|closed|no longer|not updated|historical|no new", 0),
    (r"every \d+ minutes|hourly|continual|\blive\b|daily", 365),
    (r"weekly", 52),
    (r"fortnightly", 26),
    (r"monthly", 12),
    (r"quarterly|several times a year", 4),
    (r"twice a year|half-yearly", 2),
    (r"yearly|annually|a year|school year|one or two years", 1),
    (r"census", 0.2),
)


def per_year_from_cadence(cadence: str, feed: bool = False) -> float | None:
    text = cadence.strip().lower()
    for pattern, n in CADENCE:
        if re.search(pattern, text):
            return float(n)
    return 365.0 if feed else None


@dataclass(frozen=True)
class Projection:
    slug: str
    bytes_per_version: int | None
    basis: str  # measured, estimate or unknown
    per_year: float
    per_year_basis: str  # cadence, observed, default or not publishable
    stored_versions: int
    budget_gb: float = BUDGET_GB_YEAR

    @property
    def gb_per_version(self) -> float | None:
        return None if self.bytes_per_version is None else self.bytes_per_version / GB

    @property
    def gb_per_year(self) -> float | None:
        g = self.gb_per_version
        return None if g is None else g * self.per_year

    @property
    def over_budget(self) -> bool:
        g = self.gb_per_year
        # An entry whose size cannot be worked out fails closed until a person approves it.
        return (g is None and self.per_year > 0) or (g or 0) > self.budget_gb


def versions_per_year(ds: Dataset, versions: list[str], today: dt.date) -> tuple[float, str]:
    if not ds.publishable:
        return 0.0, "not publishable"
    declared = per_year_from_cadence(ds.source.cadence, ds.source.feed)
    if declared is not None:
        return declared, "cadence"
    if not versions:
        return float(DEFAULT_PER_YEAR), "default"
    since = today - dt.timedelta(days=365)
    recent = sum(1 for v in versions if since < dt.date.fromisoformat(v) <= today)
    # A served entry that has not changed for a year is still expected to change again.
    return float(max(recent, 1)), "observed"


def catalogue_sizes(catalog: dict) -> dict[str, dict[str, int]]:
    """Bytes of the newest version's data files per slug, from the DCAT catalogue's byteSize."""
    out = {}
    for rec in catalog.get("dataset", []):
        v = rec.get("versionInfo", "")
        dists = [d for d in rec.get("distribution", []) if f"/v/{v}/" in d.get("downloadURL", "")]
        out[rec["identifier"]] = {d.get("format", ""): int(d.get("byteSize") or 0) for d in dists}
    return out


def measured_bytes(ds: Dataset, formats: dict[str, int], source_bytes: int) -> int:
    """A version's bytes in R2: its data files, the publisher's file and the partition files,
    which hold the table again as JSON (and GeoJSON for points) once per partition field."""
    total = sum(formats.values())
    if not ds.source_withheld:
        total += source_bytes
    per_part = formats.get("json", 0)
    if (ds.geometry or {}).get("kind", "point") == "point" and ds.geometry:
        per_part += formats.get("geojson", 0)
    return total + per_part * len(ds.partition_by)


def probe(url: str, timeout: float = 20) -> int | None:
    """The Content-Length of a source URL, when it answers with a file rather than a page."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if "html" in r.headers.get("Content-Type", ""):
                return None
            n = r.headers.get("Content-Length")
            return int(n) if n else None
    except OSError, ValueError:
        return None


def project(
    datasets: list[Dataset],
    store_dir: Path,
    sizes: dict[str, dict[str, int]],
    today: dt.date,
    probe_slugs: frozenset[str] = frozenset(),
    prober=probe,
) -> list[Projection]:
    out = []
    for ds in datasets:
        ms = store.manifests(store_dir, ds.slug)
        n, n_basis = versions_per_year(ds, [m.version for m in ms], today)
        newest = ms[-1] if ms else None
        if ds.slug in sizes and sizes[ds.slug]:
            b, basis = measured_bytes(ds, sizes[ds.slug], newest.bytes if newest else 0), "measured"
        elif newest is not None:
            b, basis = newest.bytes * SOURCE_MULTIPLIER, "estimate"
        elif ds.slug in probe_slugs and (src := prober(ds.source.url)):
            b, basis = src * SOURCE_MULTIPLIER, "estimate"
        else:
            b, basis = None, "unknown"
        out.append(Projection(ds.slug, b, basis, n, n_basis, len(ms)))
    return out


def r2_usd_per_month(gb: float) -> float:
    return max(0.0, gb - R2_FREE_GB) * R2_USD_PER_GB_MONTH


@dataclass(frozen=True)
class Fleet:
    stored_gb: float
    gb_per_year: float

    @property
    def usd_per_month_now(self) -> float:
        return r2_usd_per_month(self.stored_gb)

    @property
    def usd_per_month_in_a_year(self) -> float:
        return r2_usd_per_month(self.stored_gb + self.gb_per_year)

    def as_json(self) -> dict:
        return {
            "stored_gb": round(self.stored_gb, 2),
            "growth_gb_per_year": round(self.gb_per_year, 2),
            "r2_usd_per_month": round(self.usd_per_month_now, 2),
            "r2_usd_per_month_in_a_year": round(self.usd_per_month_in_a_year, 2),
        }


def fleet(projections: list[Projection]) -> Fleet:
    """Stored bytes count every stored version at its newest version's size, an estimate."""
    return Fleet(
        sum((p.gb_per_version or 0) * p.stored_versions for p in projections),
        sum(p.gb_per_year or 0 for p in projections),
    )


def fleet_from_build(outs, today: dt.date) -> Fleet:
    """The same totals from a build, where every version's file sizes are known."""
    stored = growth = 0.0
    for o in outs:
        if not o.versions:
            continue
        stored += sum(sum(v.files.values()) for v in o.versions) / GB
        n, _ = versions_per_year(o.dataset, [v.manifest.version for v in o.versions], today)
        growth += sum(o.latest.files.values()) / GB * n
    return Fleet(stored, growth)


def changed_slugs(register_dir: Path, paths: list[str], root: Path) -> set[str]:
    """Register entries among changed paths; publishers, licences and deletions are not entries."""
    out = set()
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
            out.add(full.stem)
    return out


def git_changed(base: str, root: Path) -> list[str]:
    def git(*a):
        return subprocess.run(
            ["git", *a], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    mb = git("merge-base", base, "HEAD")
    return git("diff", "--name-only", mb, "HEAD", "--", "register").splitlines()


def load_catalog(where: str) -> dict:
    if where.startswith(("http://", "https://")):
        req = urllib.request.Request(where, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    return json.loads(Path(where).read_text(encoding="utf-8"))


def _gb(x: float | None) -> str:
    return "?" if x is None else f"{x:.3f}"


def _row(p: Projection) -> str:
    basis = {"measured": "", "estimate": " (estimate)", "unknown": " (size unknown)"}[p.basis]
    return (
        f"| `{p.slug}` | {_gb(p.gb_per_version)}{basis} | {p.per_year:g} ({p.per_year_basis}) "
        f"| {_gb(p.gb_per_year)} | {'yes' if p.over_budget else 'no'} |"
    )


HEAD = (
    "| slug | GB/version | versions/yr | GB/yr | over budget |",
    "|---|---|---|---|---|",
)


def report(
    projections: list[Projection],
    gated: set[str],
    approved: bool,
    note: str = "",
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
            lines.append(f"Over budget and approved with the `{APPROVAL_LABEL}` label.")
        elif over:
            lines.append(
                f"Over budget: {', '.join(p.slug for p in over)}. A maintainer who accepts the "
                f"cost adds the `{APPROVAL_LABEL}` label, which reruns this check."
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
        "Sizes marked estimate are the source bytes times "
        f"{SOURCE_MULTIPLIER}, the fleet's measured ratio; the rest are read from the newest "
        "version's files in the catalogue.",
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
    probe_new: bool = False,
    summary: str | None = None,
) -> int:
    note = ""
    try:
        sizes = catalogue_sizes(load_catalog(catalog_src))
    except (OSError, ValueError) as e:
        sizes, note = {}, f"The catalogue could not be read ({e}), so every size is an estimate."
    changed = changed & {d.slug for d in datasets}
    projections = project(
        datasets, store_dir, sizes, today, frozenset(changed) if probe_new else frozenset()
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
                f"{p.per_year:g}/yr ({p.per_year_basis}) = {_gb(p.gb_per_year)} GB/yr"
                + (" OVER BUDGET" if p.over_budget else "")
            )
    path = summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    if over and not approved:
        print(f"cost: over budget; add the `{APPROVAL_LABEL}` label to accept the cost")
        return 1
    return 0
