"""Projected R2 storage growth and D1 rows written per register entry, and the gate on entries a
pull request changes.

Kept out of build.py's imports so it never enters the build cache key."""

from __future__ import annotations

import datetime as dt
import http.client
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from . import SITE, store
from .cadence import FEED_MAX, per_year
from .d1 import MAX_CSV, queryable
from .register import Dataset
from .serialise import pretty

GB = 10**9
BUDGET_GB_YEAR = 5.0
# D1 bills rows written past a monthly allowance, and each index on a table writes its row again.
BUDGET_D1_ROWS_YEAR = 10_000_000
# Published bytes over the publisher's bytes, measured across the fleet in October 2026: plain
# tables reach 12x at the 90th percentile and spatial ones 29x at most.
SOURCE_MULTIPLIER = 13
SPATIAL_MULTIPLIER = 30
# A zip or gzip whose unpacked size cannot be read reached 149x, and a spreadsheet 90x.
COMPRESSED_MULTIPLIER = 150
SPREADSHEET_MULTIPLIER = 90
# A gzip trailer holds the unpacked size modulo 4 GiB, which is ambiguous past this.
GZIP_TRAILER_MAX = 20 * 10**6
# Central directories larger than this are not read; the compressed multiplier applies.
ZIP_DIRECTORY_MAX = 64 * 10**6
# The fewest published bytes per row of any table in the fleet is about 205.
PUBLISHED_BYTES_PER_ROW = 200
DEFAULT_PER_YEAR = 52
# A DuckDB file's length differs from one write to the next, so the catalogue gives none and its
# bound is counted. Of 3,358 table builds none was over 1.25 times its CSV plus 600 KB of blocks,
# and the one database's file was 1.3 times its Parquet tables.
DUCKDB_PER_CSV = 1.25
DUCKDB_PER_TABLES = 2
DUCKDB_BLOCKS = 600_000
APPROVAL_LABEL = "cost-approved"
CATALOG = f"{SITE}/catalog.json"
UA = "publicdata.au cost (+https://publicdata.au/about/)"
GITHUB_API = "https://api.github.com"
ATTEMPTS = 4
SLEEP = time.sleep


class Unsized(Exception):
    """Why a source could not be sized."""


def retry(fn, attempts: int | None = None, wait: float = 1.0):
    """Calls fn until it returns, backing off on a dropped connection, a truncated body or a 5xx
    or 429; any other HTTP error is final."""
    attempts = attempts or ATTEMPTS
    for i in range(attempts):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last: Exception = e
        except (OSError, http.client.HTTPException) as e:
            last = e
        if i < attempts - 1:
            SLEEP(wait * 2**i)
    raise last


@dataclass(frozen=True)
class Sized:
    """A source file: its bytes, its unpacked bytes when they could be read, and its kind
    (plain, zip, gzip or spreadsheet)."""

    bytes: int
    kind: str = "plain"
    unpacked: int | None = None


def estimate(ds: Dataset, s: Sized) -> int:
    """A version's bytes in R2 from its source: the published files, conservatively, and the
    publisher's file once in the raw store."""
    mult = SPATIAL_MULTIPLIER if ds.geometry else SOURCE_MULTIPLIER
    if s.kind == "spreadsheet":
        published = s.bytes * max(mult, SPREADSHEET_MULTIPLIER)
    elif s.kind in ("zip", "gzip"):
        if s.unpacked is None:
            published = s.bytes * max(mult, COMPRESSED_MULTIPLIER)
        else:
            published = max(s.unpacked, s.bytes) * mult
    else:
        published = s.bytes * mult
    return published + s.bytes


def kind_of(name: str, content_type: str = "") -> str:
    n = urllib.parse.urlsplit(name).path.lower() if "://" in name else name.lower()
    ct = content_type.lower()
    if n.endswith((".xlsx", ".xlsm", ".xls", ".ods")) or "spreadsheet" in ct or "excel" in ct:
        return "spreadsheet"
    if n.endswith((".zip", ".shz", ".kmz")) or "zip" in ct and "gzip" not in ct:
        return "zip"
    if n.endswith((".gz", ".tgz")) or "gzip" in ct:
        return "gzip"
    return "plain"


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
    # Rows each version loads into D1 and the indexes on its table; None rows when unknown.
    d1_loaded: bool = False
    d1_rows: int | None = None
    d1_indexes: int = 0
    # A changed output loads the newest version into D1 again.
    d1_reload: bool = False
    note: str = ""

    @property
    def gb_per_version(self) -> float | None:
        return None if self.bytes_per_version is None else self.bytes_per_version / GB

    @property
    def gb_per_year(self) -> float | None:
        g = self.gb_per_version
        return None if g is None else g * self.per_year + self.rebuild_bytes / GB

    @property
    def d1_rows_per_year(self) -> float | None:
        if not self.d1_loaded:
            return 0.0
        if self.d1_rows is None:
            return None
        loads = self.per_year + (1 if self.d1_reload else 0)
        return self.d1_rows * (1 + self.d1_indexes) * loads

    @property
    def over_storage(self) -> bool:
        g = self.gb_per_year
        # An entry whose size cannot be worked out fails closed until a person approves it.
        return (g is None and self.per_year > 0) or (g or 0) > self.budget_gb

    @property
    def over_d1(self) -> bool:
        r = self.d1_rows_per_year
        return (r is None and self.per_year > 0) or (r or 0) > BUDGET_D1_ROWS_YEAR

    @property
    def over_budget(self) -> bool:
        return self.over_storage or self.over_d1


def versions_per_year(ds: Dataset, versions: list[str], today: dt.date) -> tuple[float, str]:
    """The larger of the declared rate and the versions stored in the last year. A new entry
    takes its cadence's rate in full; a feed is fetched daily whatever its cadence says."""
    if not ds.publishable:
        return 0.0, "not publishable"
    declared = per_year(ds.source.cadence, ds.source.feed, today)
    if ds.source.feed:
        declared = max(declared or 0.0, float(FEED_MAX))
    if not versions:
        if declared is None:
            return float(DEFAULT_PER_YEAR), "default"
        # The first fetch stores a version whatever the cadence says.
        return float(max(declared, 1)), "cadence"
    since = today - dt.timedelta(days=365)
    recent = sum(1 for v in versions if since < dt.date.fromisoformat(v) <= today)
    # An ended cadence cannot bring the rate to zero while the fetch still runs.
    if declared and declared >= recent:
        return float(declared), "cadence"
    # A served entry that has not changed for a year is still expected to change again.
    return float(max(recent, 1)), "observed"


def duckdb_bound(files: dict[str, int]) -> int:
    """The most bytes a version's data.duckdb is counted at, from the files whose size is stated."""
    tables = sum(n for p, n in files.items() if p.startswith("tables/"))
    if tables:
        return DUCKDB_PER_TABLES * tables + DUCKDB_BLOCKS
    return int(DUCKDB_PER_CSV * files.get("data.csv", 0)) + DUCKDB_BLOCKS


def catalogue_sizes(catalog: dict) -> dict[str, dict[str, int]]:
    """Bytes of each file of the newest version, by its path inside the version, per slug."""
    out = {}
    for rec in catalog.get("dataset", []):
        mark = f"/v/{rec.get('versionInfo', '')}/"
        files = {}
        for d in rec.get("distribution", []):
            url = d.get("downloadURL", "")
            if mark in url:
                files[url.split(mark, 1)[1]] = int(d.get("byteSize") or 0)
        if "data.duckdb" in files:
            files["data.duckdb"] = duckdb_bound(files)
        out[rec["identifier"]] = files
    return out


def measured_bytes(ds: Dataset, files: dict[str, int], source_bytes: int) -> int:
    """A version's bytes in R2: its data files, the publisher's file once in the raw store, and
    the partition files, which hold the table again once per partition field. NDJSON stands in
    for the partitions' JSON because data.json is not written for a large table."""
    total = sum(files.values()) + source_bytes
    rows_json = files.get("data.ndjson") or files.get("data.json", 0)
    per_part = rows_json
    if ds.geometry and (ds.geometry or {}).get("kind", "point") == "point":
        per_part += files.get("data.geojson") or rows_json
    return total + per_part * len(ds.partition_by)


def d1_indexes(ds: Dataset) -> int:
    """d1.py indexes each key and partition field once."""
    return len(dict.fromkeys((*ds.key, *ds.partition_by)))


def _open(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)


def _final(r, url: str) -> str:
    return r.geturl() if hasattr(r, "geturl") else url


def _head(url: str, timeout: float) -> tuple[int | None, str, str]:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with _open(req, timeout) as r:
        ct = r.headers.get("Content-Type", "")
        if "html" in ct:
            return None, ct, url
        n = r.headers.get("Content-Length")
        return (int(n) if n else None), ct, _final(r, url)


def _get(url: str, timeout: float) -> tuple[int | None, str, str]:
    """The headers of a GET, closed before its body is read; a Range would apply to a redirect
    itself on some hosts. Without a length, a one-byte range of the final URL gives the size."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with _open(req, timeout) as r:
        ct, n, final = r.headers.get("Content-Type", ""), r.headers.get("Content-Length"), _final(r, url)  # fmt: skip
    if "html" in ct:
        return None, ct, final
    if n:
        return int(n), ct, final
    req = urllib.request.Request(final, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    with _open(req, timeout) as r:
        total = r.headers.get("Content-Range", "").rpartition("/")[2]
        return (int(total) if total.isdigit() else None), ct, final


def _size(url: str, timeout: float) -> tuple[int | None, str, str]:
    """Bytes, type and the URL after redirects. A presigned S3 redirect refuses HEAD, so a GET's
    headers are read instead."""
    try:
        n, ct, final = retry(lambda: _head(url, timeout))
        if n:
            return n, ct, final
    except OSError, http.client.HTTPException:
        pass
    return retry(lambda: _get(url, timeout))


def _range(url: str, start: int, end: int, timeout: float) -> bytes:
    """Bytes start..end inclusive. A host that answers with the whole file is refused unread."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": f"bytes={start}-{end}"})

    def get():
        with _open(req, timeout) as r:
            if getattr(r, "status", 206) != 206:
                raise Unsized("the host does not serve byte ranges")
            body = r.read(end - start + 1)
            if len(body) != end - start + 1:
                raise http.client.IncompleteRead(body, end - start + 1 - len(body))
            return body

    return retry(get)


class _RangeFile(io.RawIOBase):
    """A remote file read by byte ranges, enough for zipfile to read the central directory."""

    def __init__(self, url: str, size: int, timeout: float):
        self.url, self.size, self.timeout, self.pos = url, size, timeout, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = {0: 0, 1: self.pos, 2: self.size}[whence] + offset
        return self.pos

    def read(self, n=-1):
        end = self.size if n is None or n < 0 else min(self.size, self.pos + n)
        if end - self.pos > ZIP_DIRECTORY_MAX:
            raise Unsized("the zip's central directory is too large to read")
        if end <= self.pos:
            return b""
        body = _range(self.url, self.pos, end - 1, self.timeout)
        self.pos = end
        return body


def unpacked_bytes(url: str, size: int, kind: str, timeout: float = 30) -> int | None:
    """A zip's members' unpacked bytes from its central directory, or a small gzip's from its
    trailer. A member that is itself compressed is counted at the compressed multiplier."""
    try:
        if kind == "zip":
            with zipfile.ZipFile(_RangeFile(url, size, timeout)) as z:
                total = 0
                for i in z.infolist():
                    if kind_of(i.filename) in ("zip", "gzip"):
                        total += i.file_size * COMPRESSED_MULTIPLIER
                    else:
                        total += i.file_size
                return total
        if kind == "gzip" and 4 <= size <= GZIP_TRAILER_MAX:
            n = int.from_bytes(_range(url, size - 4, size - 1, timeout), "little")
            while n < size:
                n += 2**32
            return n
    except OSError, http.client.HTTPException, zipfile.BadZipFile, Unsized, ValueError:
        return None
    return None


def _sized(url: str, timeout: float, hint: str = "") -> Sized:
    # urllib would also open a file: URL on the runner.
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise Unsized(f"{url} is not an http or https URL")
    n, ct, final = _size(url, timeout)
    if not n:
        raise Unsized(f"{url} gave no size")
    kind = kind_of(hint) if kind_of(hint) != "plain" else kind_of(url, ct)
    return Sized(n, kind, unpacked_bytes(final, n, kind, timeout) if kind != "plain" else None)


def probe(ds: Dataset, timeout: float = 30) -> Sized:
    """The file the fetch would read: the CKAN resource the entry resolves to, the way the fetch
    picks it, or a fixed file URL. Other adapters are not sized and raise Unsized."""
    try:
        if ds.source.adapter == "ckan-resource":
            import requests

            from .fetch import _package, pick_resource

            s = requests.Session()
            s.headers["User-Agent"] = UA
            api = f"{ds.source.portal.rstrip('/')}/api/3/action"
            res = pick_resource(ds, retry(lambda: _package(ds, s, api))["resources"])
            hint = f"x.{str(res.get('format') or '').lower()}" if res.get("format") else ""
            hint = hint if kind_of(hint) != "plain" else str(res.get("url") or "")
            # Some portals give the size as text ("2 MiB"); the host's headers are exact.
            if str(res.get("size") or "").isdigit() and kind_of(hint) == "plain":
                return Sized(int(res["size"]))
            return _sized(res["url"], timeout, hint)
        if ds.source.adapter == "file" and not ds.source.page_size:
            return _sized(ds.source.url, timeout, ds.source.format and f"x.{ds.source.format}")
    except Unsized:
        raise
    except Exception as e:  # noqa: BLE001
        raise Unsized(f"the source could not be read ({type(e).__name__}: {e})") from e
    raise Unsized(f"the {ds.source.adapter} adapter's source is not sized ahead of a fetch")


def live_rows(slugs: list[str], site: str = SITE, workers: int = 16) -> dict[str, int]:
    """Rows of each dataset's newest version, from its versions.json; a slug that cannot be read
    is left out."""

    def one(slug: str) -> tuple[str, int | None]:
        req = urllib.request.Request(f"{site}/d/{slug}/versions.json", headers={"User-Agent": UA})

        def get():
            with _open(req, 60) as r:
                return json.load(r)

        try:
            doc = retry(get)
        except OSError, http.client.HTTPException, ValueError:
            return slug, None
        latest = doc.get("latest")
        return slug, next(
            (int(v["rows"]) for v in doc.get("versions", []) if v.get("version") == latest), None
        )

    with ThreadPoolExecutor(workers) as ex:
        return {s: n for s, n in ex.map(one, slugs) if n is not None}


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
    rows: dict[str, int] | None = None,
) -> list[Projection]:
    """`sizes` is None when the catalogue could not be read; a changed entry is then unknown.
    `fresh` are changed entries whose source or output shape moved: sized from the new file,
    and never below what the newest version measures. `reshaped` holds the base copy of each
    entry whose output shape changed, so the rebuild of its stored versions is counted. `rows`
    are each served entry's rows per version."""
    reshaped = reshaped or {}
    rows = rows or {}
    out = []
    for ds in datasets:
        ms = store.manifests(store_dir, ds.slug)
        n, n_basis = versions_per_year(ds, [m.version for m in ms], today)
        newest = ms[-1] if ms else None
        b, basis, note = None, "unknown", ""
        measured = None
        files = (sizes or {}).get(ds.slug) or {}
        if files:
            measured = measured_bytes(ds, files, newest.bytes if newest else 0)
        if sizes is None and ds.slug in changed and ms:
            note = "the catalogue could not be read"
        elif ds.slug in fresh or (ds.slug in changed and not ms):
            if not probing:
                note = "the source was not probed"
            else:
                try:
                    b, basis = estimate(ds, prober(ds)), "estimate"
                except Unsized as e:
                    note = str(e)
                if b is not None and measured is not None and measured >= b:
                    b, basis = measured, "measured"
        elif measured is not None:
            b, basis = measured, "measured"
        elif newest is not None:
            b, basis = estimate(ds, Sized(newest.bytes, kind_of(newest.filename))), "estimate"
        rebuild = 0
        if ds.slug in reshaped and ms and b is not None:
            old = reshaped[ds.slug]
            before = replace(
                ds,
                partition_by=tuple(old.get("partition_by") or ()),
                geometry=old.get("geometry") or None,
            )
            was = (
                measured_bytes(before, files, newest.bytes)
                if measured is not None
                else estimate(before, Sized(newest.bytes, kind_of(newest.filename)))
            )
            rebuild = max(0, b - was) * len(ms)
        csv = files.get("data.csv")
        loaded = (
            ds.publishable and ds.query and not (csv and csv > MAX_CSV and ds.slug not in fresh)
        )
        r = rows.get(ds.slug)
        if b is not None and (r is None or ds.slug in fresh):
            r = max(r or 0, b // PUBLISHED_BYTES_PER_ROW)
        out.append(
            Projection(
                ds.slug,
                b,
                basis,
                n,
                n_basis,
                len(ms),
                rebuild_bytes=rebuild,
                d1_loaded=bool(loaded),
                d1_rows=r,
                d1_indexes=d1_indexes(ds),
                d1_reload=ds.slug in reshaped and bool(ms),
                note=note,
            )
        )
    return out


# What /health.json's storage figures cover, so a reader can tell the estimate from the measurement.
PROJECTED_COVERS = (
    "An estimate from the build: every version's files in publicdata-dist and its publisher's file "
    "once in publicdata-raw, with a year's growth at each entry's newest version times the versions "
    "its cadence and history give a year. It leaves out the build cache and the query copies."
)
MEASURED_COVERS = (
    "Cloudflare's own measurement of every object in publicdata-dist and publicdata-raw, the build "
    "cache and the query copies included."
)
MEASURED_BUCKETS = ("publicdata-dist", "publicdata-raw")
GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
# One newest reading per bucket, so the account's other buckets and the sampling rate never crowd
# it out.
_R2_BUCKET = (
    "b{i}: r2StorageAdaptiveGroups(limit: 1, orderBy: [datetime_DESC], filter: "
    '{{bucketName: "{b}", datetime_geq: $since}}) '
    "{{ max {{ payloadSize metadataSize objectCount }} dimensions {{ datetime }} }}"
)
R2_STORAGE = (
    "query ($account: string!, $since: Time!) { viewer { accounts(filter: {accountTag: $account}) { "
    + " ".join(_R2_BUCKET.format(i=i, b=b) for i, b in enumerate(MEASURED_BUCKETS))
    + " } } }"
)


@dataclass(frozen=True)
class Fleet:
    stored_bytes: int
    bytes_per_year: int
    d1_rows_per_year: int = 0

    @property
    def stored_gb(self) -> float:
        return self.stored_bytes / GB

    @property
    def gb_per_year(self) -> float:
        return self.bytes_per_year / GB

    def as_json(self) -> dict:
        return {
            "projected": {
                "covers": PROJECTED_COVERS,
                "stored_bytes": self.stored_bytes,
                "stored_bytes_in_a_year": self.stored_bytes + self.bytes_per_year,
                "growth_bytes_per_year": self.bytes_per_year,
                "d1_rows_written_per_year": self.d1_rows_per_year,
            },
            "measured": unmeasured("measured only by a deploy to production"),
        }


def fleet(projections: list[Projection]) -> Fleet:
    """Stored bytes count every stored version at its newest version's size, an estimate."""
    return Fleet(
        sum((p.bytes_per_version or 0) * p.stored_versions for p in projections),
        round(sum((p.bytes_per_version or 0) * p.per_year + p.rebuild_bytes for p in projections)),
        round(sum(p.d1_rows_per_year or 0 for p in projections)),
    )


def unmeasured(reason: str) -> dict:
    return {"available": False, "reason": reason}


class Unmeasured(Exception):
    """A reason the measurement is unavailable, worded to be published."""


def measure_r2(account: str, token: str, now: dt.datetime) -> dict:
    """The newest stored bytes Cloudflare reports for each archive bucket. The token needs Account
    Analytics Read."""
    since = (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = json.dumps({"query": R2_STORAGE, "variables": {"account": account, "since": since}})
    req = urllib.request.Request(
        GRAPHQL,
        data=body.encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )

    def post():
        with _open(req, 30) as r:
            return json.load(r)

    try:
        doc = retry(post)
    except urllib.error.HTTPError as e:
        raise Unmeasured(f"the GraphQL Analytics API answered HTTP {e.code}") from e
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise Unmeasured("the GraphQL Analytics API could not be read") from e
    if doc.get("errors"):
        raise Unmeasured("the GraphQL Analytics API refused the query")
    accounts = ((doc.get("data") or {}).get("viewer") or {}).get("accounts") or []
    if not accounts:
        raise Unmeasured("the token cannot read the account's analytics")
    newest = {b: (accounts[0].get(f"b{i}") or [None])[0] for i, b in enumerate(MEASURED_BUCKETS)}
    if missing := [b for b, g in newest.items() if not g]:
        raise Unmeasured(f"no storage reading for {', '.join(missing)} since {since}")
    buckets = {
        b: int(g["max"]["payloadSize"]) + int(g["max"]["metadataSize"]) for b, g in newest.items()
    }
    return {
        "available": True,
        "covers": MEASURED_COVERS,
        "measured_at": min(g["dimensions"]["datetime"] for g in newest.values()),
        "stored_bytes": sum(buckets.values()),
        "objects": sum(int(g["max"]["objectCount"]) for g in newest.values()),
        "buckets": buckets,
    }


def stamp_health(path: Path, measure) -> dict:
    """Writes the measured figure into a built health.json. Only an Unmeasured reason is published;
    any other failure is logged and published as unreadable, so it never stops a deploy."""
    try:
        measured = measure()
    except Unmeasured as e:
        measured = unmeasured(str(e))
    except Exception as e:  # noqa: BLE001
        print(f"measure: {type(e).__name__}: {e}", file=sys.stderr)
        measured = unmeasured("the measurement could not be read")
    health = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(health.get("storage"), dict):
        health["storage"] = {}
    health["storage"]["measured"] = measured
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(pretty(health), encoding="utf-8")
    os.replace(tmp, path)
    return measured


def build_version_bytes(ds: Dataset, v) -> int:
    """A built version's bytes in R2: every file it lists, and the publisher's file in the raw
    store, which a withheld source keeps without listing. A DuckDB file counts at its bound, as
    the catalogue would give it, so two builds of one snapshot state the same total."""
    files = {k: n for k in v.files if (n := v.size(k)) is not None}
    if "data.duckdb" in v.files:
        files["data.duckdb"] = duckdb_bound(files)
    return sum(files.values()) + (v.manifest.bytes if ds.source_withheld else 0)


def fleet_from_build(outs, today: dt.date) -> Fleet:
    """The same totals from a build, where every version's file sizes and rows are known."""
    stored = growth = rows = 0
    for o in outs:
        if not o.versions:
            continue
        stored += sum(build_version_bytes(o.dataset, v) for v in o.versions)
        n, _ = versions_per_year(o.dataset, [v.manifest.version for v in o.versions], today)
        growth += build_version_bytes(o.dataset, o.latest) * n
        if queryable(o.dataset, o.latest.files.get("data.csv")):
            rows += o.latest.rows * (1 + d1_indexes(o.dataset)) * n
    return Fleet(stored, round(growth), round(rows))


def _git(root: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def changed_paths(base: str, root: Path) -> tuple[str, list[str]]:
    mb = _git(root, "merge-base", base, "HEAD")
    return mb, _git(root, "diff", "--no-renames", "--name-only", mb, "HEAD", "--", "register").splitlines()  # fmt: skip


def symlinks(root: Path) -> list[str]:
    """Symbolic links in the register, committed or on disk. An entry reached through one could
    change without its path changing, so the check refuses them."""
    found = {
        line.split("\t", 1)[1]
        for line in _git(root, "ls-tree", "-r", "HEAD", "--", "register").splitlines()
        if line.startswith("120000 ")
    }
    reg = root / "register"
    if reg.is_symlink():
        found.add("register")
    elif reg.is_dir():
        for d, dirs, names in os.walk(reg):
            for name in dirs + names:
                p = Path(d) / name
                if p.is_symlink():
                    found.add(p.relative_to(root).as_posix())
    return sorted(found)


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


# Keys besides the source and the shape that a projection reads: whether the entry is
# published, whether D1 loads it and what D1 indexes.
COST_KEYS = ("partition_by", "key", "query", "status", "licence")


def _costs(raw: dict) -> tuple:
    src = raw.get("source") or {}
    return (
        _source(raw),
        _shape(raw),
        src.get("cadence"),
        src.get("feed"),
        {k: raw.get(k) for k in COST_KEYS},
    )


def costed(root: Path, base: str, entries: dict[str, str]) -> set[str]:
    """Changed entries whose edit can move what they cost: new ones, and those whose source,
    cadence, shape or a key in COST_KEYS differs from the base's. A description, a label or a
    raised rebuild number leaves the projection as it was, so the gate does not ask for an
    approval it already had."""
    out = set()
    base_paths = _base_paths(root, base)
    for slug, path in entries.items():
        if slug not in base_paths:
            out.add(slug)
            continue
        new = yaml.safe_load((root / path).read_text(encoding="utf-8")) or {}
        old = yaml.safe_load(_git(root, "show", f"{base}:{base_paths[slug]}")) or {}
        if _costs(old) != _costs(new):
            out.add(slug)
    return out


def load_catalog(where: str) -> dict:
    if where.startswith(("http://", "https://")):
        req = urllib.request.Request(where, headers={"User-Agent": UA})

        def get():
            with _open(req, 60) as r:
                return json.load(r)

        return retry(get)
    return json.loads(Path(where).read_text(encoding="utf-8"))


def _github(path: str, token: str):
    req = urllib.request.Request(
        f"{GITHUB_API}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    def get():
        with _open(req, 30) as r:
            return json.load(r)

    return retry(get)


def _pages(get, path: str, key: str | None = None, limit: int = 30) -> list:
    out = []
    sep = "&" if "?" in path else "?"
    for page in range(1, limit + 1):
        doc = get(f"{path}{sep}per_page=100&page={page}")
        items = doc[key] if key else doc
        out += items
        if len(items) < 100:
            break
    return out


WRITE_ROLES = ("admin", "maintain", "write")


def approval(repo: str, pr: int, get) -> tuple[bool, str]:
    """Whether the pull request's newest commit carries an approval: the label is on, it was
    last added by someone with write access who did not open the pull request, after the head
    commit arrived and after any change of base. `get` reads a GitHub API path."""
    pull = get(f"repos/{repo}/pulls/{pr}")
    if APPROVAL_LABEL not in {lb["name"] for lb in pull.get("labels", [])}:
        return False, "the label is not on the pull request"
    author, head = pull["user"]["login"], pull["head"]["sha"]
    last, rebased = None, ""
    for e in _pages(get, f"repos/{repo}/issues/{pr}/events"):
        if (
            e.get("event") in ("labeled", "unlabeled")
            and (e.get("label") or {}).get("name") == APPROVAL_LABEL
        ):
            last = e
        elif e.get("event") == "base_ref_changed":
            rebased = max(rebased, e["created_at"])
    if not last or last["event"] != "labeled":
        return False, "no labelling event was found"
    actor, at = (last.get("actor") or {}).get("login", ""), last["created_at"]
    if not actor or actor == author:
        return False, "the label was added by the pull request's author"
    try:
        perm = get(f"repos/{repo}/collaborators/{urllib.parse.quote(actor)}/permission")
    except urllib.error.HTTPError:
        perm = {}
    if perm.get("role_name") not in WRITE_ROLES and perm.get("permission") not in WRITE_ROLES:
        return False, f"{actor} has no write access"
    if rebased and at <= rebased:
        return False, "the label predates a change of base branch"
    since = head_since(repo, pull, get)
    if since is None or at <= since:
        return False, "the label predates the newest commit"
    return True, f"approved by {actor} for {head[:7]}"


def head_since(repo: str, pull: dict, get, workflow: str = "cost.yml") -> str | None:
    """When the pull request's head became its current commit: the first of this check's runs in
    the unbroken run of runs on that commit, newest first. Run times are the server's, where a
    commit's own date is whatever its author wrote."""
    head = pull["head"]
    branch = urllib.parse.quote(head["ref"], safe="")
    path = f"repos/{repo}/actions/workflows/{workflow}/runs?event=pull_request_target&branch={branch}"  # fmt: skip
    since = None
    for r in _pages(get, path, "workflow_runs", limit=10):
        if (r.get("head_repository") or {}).get("full_name") != (head.get("repo") or {}).get(
            "full_name"
        ):
            continue
        if r["head_sha"] != head["sha"]:
            break
        since = r["created_at"]
    return since


def _gb(x: float | None) -> str:
    return "?" if x is None else f"{x:.3f}"


def _rows(x: float | None) -> str:
    return "?" if x is None else f"{x:,.0f}"


BASIS = {"measured": "", "estimate": " (estimate)", "unknown": " (size unknown)"}


def _row(p: Projection) -> str:
    return (
        f"| `{p.slug}` | {_gb(p.gb_per_version)}{BASIS[p.basis]} | {p.per_year:g} ({p.per_year_basis}) "
        f"| {_gb(p.rebuild_bytes / GB)} | {_gb(p.gb_per_year)} | {_rows(p.d1_rows_per_year)} "
        f"| {'yes' if p.over_budget else 'no'} |"
    )


HEAD = (
    "| slug | GB/version | versions/yr | rebuild GB | GB/yr | D1 rows written/yr | over budget |",
    "|---|---|---|---|---|---|---|",
)


def report(
    projections: list[Projection],
    gated: set[str],
    approved: bool,
    note: str = "",
    why: str = "",
) -> tuple[str, list[Projection]]:
    """The Markdown summary and the gated entries over budget."""
    f = fleet(projections)
    over = [p for p in projections if p.slug in gated and p.over_budget]
    lines = ["## Storage cost", ""]
    if note:
        lines += [note, ""]
    if gated:
        lines += [
            f"Entries this change adds or edits, against {BUDGET_GB_YEAR:g} GB of storage and "
            f"{BUDGET_D1_ROWS_YEAR:,} D1 rows written a year each:",
            "",
        ]
        lines += [*HEAD, *(_row(p) for p in projections if p.slug in gated), ""]
        lines += [f"- `{p.slug}`: {p.note}." for p in projections if p.slug in gated and p.note]
        if any(p.note for p in projections if p.slug in gated):
            lines.append("")
        if over and approved:
            lines.append(f"Over budget and approved with the `{APPROVAL_LABEL}` label: {why}.")
        elif over:
            lines.append(
                f"Over budget: {', '.join(p.slug for p in over)}. A maintainer other than the "
                f"pull request's author who accepts the cost adds the `{APPROVAL_LABEL}` label, "
                "which reruns this check. The label approves only the commit it was added on, so "
                "a later push or a change of base needs it added again."
                + (f" Not approved: {why}." if why else "")
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
        f"Projected D1 rows written: {f.d1_rows_per_year:,} a year, each version's rows once "
        "for the table and once for each index on it.",
        "",
        "A change to an entry's output rebuilds each of its stored versions once, counted at the "
        "newest version's size in the rebuild column and in the year's figure, and loads its "
        "newest version into D1 again. Sizes marked estimate are the source bytes times "
        f"{SOURCE_MULTIPLIER} ({SPATIAL_MULTIPLIER} for a spatial entry), with a zip or gzip "
        "counted at its unpacked size, or at "
        f"{COMPRESSED_MULTIPLIER} times its bytes when that cannot be read, and a spreadsheet at "
        f"{SPREADSHEET_MULTIPLIER} times. The rest are read from the newest version's files in "
        "the catalogue, with the publisher's file counted once for the raw store. D1 rows are "
        "the newest version's, or one per "
        f"{PUBLISHED_BYTES_PER_ROW} published bytes for an estimate.",
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
    approve=None,
    rows: dict[str, int] | None = None,
    prober=probe,
) -> int:
    """`approve` is asked only when a changed entry is over budget, and returns (approved, why).
    `rows` defaults to reading each served entry's rows from the site."""
    known = {d.slug for d in datasets}
    if missing := sorted(changed - known):
        print(f"cost: no register entry named {', '.join(missing)}")
        return 2
    note = ""
    try:
        sizes = catalogue_sizes(load_catalog(catalog_src))
    except (OSError, ValueError, http.client.HTTPException) as e:
        sizes = None
        note = (
            f"The catalogue could not be read ({e}). Changed entries count as size unknown and "
            "the rest are estimates."
        )
    if rows is None:
        rows = live_rows(sorted(s for s in (sizes or {}) if s in known)) if sizes else {}
    projections = project(
        datasets,
        store_dir,
        sizes,
        today,
        frozenset(changed),
        frozenset(fresh),
        prober=prober,
        probing=probing,
        reshaped=reshaped,
        rows=rows,
    )
    over = [p for p in projections if p.slug in changed and p.over_budget]
    why = ""
    if over and not approved and approve is not None:
        approved, why = approve()
    text, over = report(projections, changed, approved, note, why)
    f = fleet(projections)
    print(
        f"cost: {f.gb_per_year:,.1f} GB a year projected over {len(projections)} entries; "
        f"{f.stored_gb:,.1f} GB stored (estimated), {f.stored_gb + f.gb_per_year:,.1f} GB in a "
        f"year; {f.d1_rows_per_year:,} D1 rows written a year"
    )
    for p in projections:
        if p.slug in changed:
            print(
                f"cost: {p.slug} {_gb(p.gb_per_version)} GB/version ({p.basis}) x "
                f"{p.per_year:g}/yr ({p.per_year_basis}) + {_gb(p.rebuild_bytes / GB)} GB rebuild "
                f"= {_gb(p.gb_per_year)} GB/yr; {_rows(p.d1_rows_per_year)} D1 rows/yr"
                + (" OVER BUDGET" if p.over_budget else "")
                + (f" ({p.note})" if p.note else "")
            )
    path = summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    if over and not approved:
        print(
            f"cost: over budget; a maintainer other than the author adds the `{APPROVAL_LABEL}` "
            "label to accept the cost" + (f" ({why})" if why else "")
        )
        return 1
    if over:
        print(f"cost: over budget, approved ({why or 'by flag'})")
    return 0
