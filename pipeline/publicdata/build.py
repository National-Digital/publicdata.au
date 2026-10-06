"""Turn the register and the raw store into dist/. Never touches upstream: a cached version's
Parquet comes back through published, from the tree or R2 that holds it."""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import zstandard

from . import OPERATOR, SITE, published, store
from .cache import BuildCache, _link_or_copy
from .diff import diff
from .normalise import Table, normalise
from .provenance import OPERATOR_URL
from .provenance import header as prov_header
from .register import Dataset
from .serialise import (
    MEDIA,
    WRITERS,
    csvw_metadata,
    formats_for,
    pretty,
    schema_sql,
    table_schema,
    write_partitions,
)
from .serialise.geo import geo_kind
from .serialise.profile import (
    layout,
    order_of,
    permutation,
    query_key,
    sha256,
    signature,
    widen,
)

REGISTER_DIR = Path(__file__).resolve().parents[2] / "register"


@dataclass
class VersionOut:
    manifest: store.Manifest
    rows: int
    files: dict[str, int]  # relative path -> bytes
    unknown_columns: list[str]
    suppressed_cells: int
    partitions: dict
    first: str = ""  # the first row of data.ndjson, which the dataset page shows
    absent: tuple[str, ...] = ()  # files a cached build left out; already published
    tables: dict[str, int] = field(default_factory=dict)  # a database's tables and their rows
    query: str = ""  # the tree path of a table version's query copy (serialise.profile)


# What a cached version keeps: the small files a later build reads back, for the gate and the
# dataset schema. The rest are written once to R2 by the build that made them, and the Parquet
# a diff, the history archive or a page reads is fetched back from there.
KEPT = re.compile(r"^(manifest\.json|schema\.json|schema\.sql)$")


def source_name(ds: Dataset, m: store.Manifest) -> str:
    """The publisher's file as a version lists it. Its bytes stay in the raw store, which the /d/
    function serves it from, so no built tree holds it."""
    return "" if ds.source_withheld else f"source.{m.ext}"


def _with_source(files: dict[str, int], ds: Dataset, m: store.Manifest) -> dict[str, int]:
    if not (name := source_name(ds, m)):
        return files
    return dict(sorted({**files, name: m.bytes}.items(), key=lambda kv: Path(kv[0])))


def kept(rel: str) -> bool:
    return bool(KEPT.match(rel))


@dataclass
class DatasetOut:
    dataset: Dataset
    versions: list[VersionOut] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)

    @property
    def latest(self) -> VersionOut | None:
        return self.versions[-1] if self.versions else None


def dataset_url(slug: str) -> str:
    return f"{SITE}/d/{slug}/"


def version_url(slug: str, version: str) -> str:
    return f"{SITE}/d/{slug}/v/{version}/"


def _size(p: Path) -> int:
    """A file's size in bytes. A DuckDB file's size differs from one write to the next, since
    its storage lays out blocks by sampling, so it is given to one significant figure and two
    builds of one snapshot still agree."""
    n = p.stat().st_size
    if p.name != "data.duckdb" or n < 10:
        return n
    scale = 10 ** (len(str(n)) - 1)
    return round(n / scale) * scale


def _sizes(root: Path) -> dict[str, int]:
    return {
        str(p.relative_to(root)).replace("\\", "/"): _size(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def build_database_version(
    ds: Dataset, m: store.Manifest, src: Path, out: Path
) -> tuple[None, VersionOut]:
    """A database version: the archive's tables as one DuckDB file and one Parquet per table,
    with the schema, the script, the publisher's archive and the manifest beside them."""
    from .database import build_database

    vdir = out / "d" / ds.slug / "v" / m.version
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    base = version_url(ds.slug, m.version)

    def hdr(rows: int, rel: str) -> dict:
        return prov_header(ds, m, rows, base + rel)

    db = build_database(ds, m, src, vdir, hdr)
    man = json.loads(m.to_json())
    man["kind"] = "database"
    man["rows"] = db.rows
    man["fields"] = ds.field_count
    man["tables"] = db.tables
    man["unknown_upstream_columns"] = [
        f"{t}.{c}" for t, cols in sorted(db.unknown_columns.items()) for c in cols
    ]
    man["unknown_upstream_tables"] = db.unknown_tables
    man["suppressed_cells"] = 0
    man["url"] = base
    (vdir / "manifest.json").write_text(pretty(man), encoding="utf-8")
    return None, VersionOut(
        m,
        db.rows,
        _with_source(_sizes(vdir), ds, m),
        man["unknown_upstream_columns"] + man["unknown_upstream_tables"],
        0,
        {},
        tables=db.tables,
    )


def write_formats(tbl: Table, fmts: list[str], hdr, vdir: Path) -> None:
    """Each format's file, in the order FORMATS lists them, so the gzip finds the CSV."""
    for fmt in fmts:
        WRITERS[fmt](tbl, hdr(tbl.rows, f"data.{fmt}"), vdir / f"data.{fmt}", vdir)


def build_version(
    ds: Dataset, m: store.Manifest, data: bytes, out: Path, store_dir: Path | None = None
) -> tuple[Table, VersionOut]:
    tbl = normalise(ds, m, data)
    if ds.enrich:
        from .spine import enrich

        if store_dir is None:
            raise ValueError(f"{ds.slug}: joining the place spine needs the store")
        tbl = enrich(tbl, store_dir, REGISTER_DIR)
    tbl = _sorted_once(tbl, m.parquet)
    vdir = out / "d" / ds.slug / "v" / m.version
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    base = version_url(ds.slug, m.version)

    def hdr(rows: int, rel: str) -> dict:
        return prov_header(ds, m, rows, base + rel)

    fmts = formats_for(tbl.rows, geo_kind(ds))
    if "ndjson" not in fmts:
        raise ValueError("every build writes data.ndjson, which the dataset page reads back")
    write_formats(tbl, fmts, hdr, vdir)
    query = _query_copy(tbl, hdr(tbl.rows, "data.parquet"), vdir, out)
    partitions = write_partitions(tbl, hdr, vdir)
    (vdir / "schema.json").write_text(pretty(table_schema(tbl)), encoding="utf-8")
    (vdir / "schema.sql").write_text(schema_sql(tbl, hdr(tbl.rows, "schema.sql")), encoding="utf-8")
    (vdir / "data.csv-metadata.json").write_text(
        pretty(csvw_metadata(tbl, hdr(tbl.rows, "data.csv-metadata.json"))), encoding="utf-8"
    )
    man = json.loads(m.to_json())
    if ds.source_withheld:
        man["source_withheld"] = ds.source_withheld
    man["rows"] = tbl.rows
    man["fields"] = len(ds.fields)
    man["unknown_upstream_columns"] = tbl.unknown_columns
    man["suppressed_cells"] = tbl.suppressed_cells
    if tbl.short_rows:
        man["short_rows_padded"] = tbl.short_rows
    if tbl.omitted_columns:
        man["omitted_upstream_columns"] = {c: ds.omit[c] for c in tbl.omitted_columns}
    if tbl.places:
        man["places"] = tbl.places
    man["url"] = base
    (vdir / "manifest.json").write_text(pretty(man), encoding="utf-8")
    return tbl, VersionOut(
        m,
        tbl.rows,
        _with_source(_sizes(vdir), ds, m),
        tbl.unknown_columns,
        tbl.suppressed_cells,
        partitions,
        _first_row(vdir),
        query=query,
    )


def _sorted_once(tbl: Table, lay: dict) -> Table:
    """tbl carrying its permutation under layout `lay`, so every writer that sorts takes it."""
    if not lay or not lay.get("sort"):
        return tbl
    perm = permutation(tbl.table, lay["sort"], lay["key"])
    return replace(tbl, order=(tbl.table, (tuple(lay["sort"]), tuple(lay["key"])), perm))


def _query_copy(tbl: Table, header: dict, vdir: Path, out: Path) -> str:
    """The version's query copy under the current profile and register entry, at its internal
    key: the version's own data.parquet when that already follows them, else written again.
    Returns its path in the tree."""
    from .serialise.writers.geo_parquet import write_shape_parquet
    from .serialise.writers.parquet import write_parquet

    ds, m = tbl.dataset, tbl.manifest
    rel = query_key(ds.slug, m.version)
    p = out / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.unlink(missing_ok=True)
    lay = layout(ds)
    if m.parquet == lay and (vdir / "data.parquet").is_file():
        _link_or_copy(str(vdir / "data.parquet"), str(p))
        return rel
    q = _sorted_once(tbl, lay)
    (write_shape_parquet if q.geometry is not None else write_parquet)(q, header, p, lay=lay)
    return rel


def _first_row(vdir: Path) -> str:
    """The first record of data.ndjson, after the header line."""
    with (vdir / "data.ndjson").open(encoding="utf-8") as f:
        f.readline()
        return f.readline().rstrip("\n")


def _history(dout: DatasetOut, out: Path, cache: BuildCache | None, keys: list[str]) -> None:
    ddir = out / "d" / dout.dataset.slug
    key = cache.key(*keys, "history") if cache else ""
    if cache and cache.get(key) is not None:
        shutil.copyfile(cache.root / key / "files" / "history.tar.zst", ddir / "history.tar.zst")
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        kept = (
            ("manifest.json", "schema.json")
            if dout.dataset.kind == "database"
            else ("manifest.json", "data.parquet")
        )
        for v in dout.versions:
            for name in kept:
                p = published.path(out, f"d/{dout.dataset.slug}/v/{v.manifest.version}/{name}")
                ti = tarfile.TarInfo(f"{dout.dataset.slug}/{v.manifest.version}/{name}")
                ti.size = p.stat().st_size
                ti.mtime = 0
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                with p.open("rb") as f:
                    tar.addfile(ti, f)
    c = zstandard.ZstdCompressor(level=10, write_checksum=False)
    (ddir / "history.tar.zst").write_bytes(c.compress(buf.getvalue()))
    if cache:
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copyfile(ddir / "history.tar.zst", Path(tmp) / "history.tar.zst")
            cache.put(key, {}, Path(tmp))


def _datapackage(dout: DatasetOut) -> dict:
    ds, v = dout.dataset, dout.latest
    m = v.manifest
    base = version_url(ds.slug, m.version)
    h = prov_header(ds, m, v.rows, base)
    resources = []
    if ds.kind == "database":
        resources.append(
            {
                "name": "duckdb",
                "path": base + "data.duckdb",
                "format": "duckdb",
                "mediatype": MEDIA["duckdb"],
                "bytes": v.files.get("data.duckdb"),
                "schema": f"{base}schema.json",
                "description": f"Every table and view in one DuckDB database. Attach it read-only over HTTPS or download it. {len(ds.tables)} tables. The size is to one significant figure.",
            }
        )
        for t in ds.tables:
            name = f"tables/{t.name}.parquet"
            resources.append(
                {
                    "name": t.name,
                    "path": base + name,
                    "format": "parquet",
                    "mediatype": MEDIA["parquet"],
                    "bytes": v.files.get(name),
                    "schema": f"{base}schema.json#/tables/{t.name}",
                    "description": t.description or t.source,
                    "publicdata:rows": v.tables.get(t.name),
                }
            )
    else:
        for fmt in formats_for(v.rows, geo_kind(ds)):
            name = f"data.{fmt}"
            resources.append(
                {
                    "name": fmt,
                    "path": base + name,
                    "format": fmt,
                    "mediatype": MEDIA[fmt],
                    "bytes": v.files.get(name),
                    "schema": f"{base}schema.json",
                }
            )
    resources.append(
        {
            "name": "schema-sql",
            "path": base + "schema.sql",
            "format": "sql",
            "mediatype": MEDIA["sql"],
            "bytes": v.files.get("schema.sql"),
            "description": "CREATE TABLE for PostgreSQL and most SQL dialects"
            + (
                ", with keys, references and the views."
                if ds.kind == "database"
                else ", with a COPY line."
            ),
        }
    )
    if ds.kind != "database":
        resources.append(
            {
                "name": "csvw",
                "path": base + "data.csv-metadata.json",
                "format": "json",
                "mediatype": MEDIA["csv-metadata.json"],
                "bytes": v.files.get("data.csv-metadata.json"),
                "description": "W3C CSV on the Web metadata for data.csv.",
            }
        )
    if not ds.source_withheld:
        resources.append(
            {
                "name": "source",
                "path": base + f"source.{m.ext}",
                "format": m.ext,
                "bytes": m.bytes,
                "hash": f"sha256:{m.sha256}",
                "description": "The publisher's file, byte for byte as fetched.",
            }
        )
    return {
        "$schema": "https://datapackage.org/profiles/2.0/datapackage.json",
        "name": ds.slug,
        "title": ds.title,
        "description": ds.summary or ds.description,
        "homepage": dataset_url(ds.slug),
        "version": m.version,
        "licenses": [
            {
                "name": ds.licence.id,
                "path": ds.licence.url,
                "title": ds.licence.title,
                **({"publicdata:condition": ds.licence.condition} if ds.licence.condition else {}),
            }
        ],
        **(
            {"publicdata:kind": "database", "publicdata:tables": list(v.tables)}
            if ds.kind == "database"
            else {}
        ),
        "sources": [{"title": ds.publisher.name, "path": m.source.get("url")}],
        "contributors": [
            {"title": ds.publisher.name, "role": "publisher"},
            {"title": OPERATOR, "path": OPERATOR_URL, "role": "wrangler"},
        ],
        "publicdata:attribution": h["attribution"],
        "publicdata:cite": h["cite"],
        "publicdata:notEndorsed": True,
        "resources": resources,
    }


def version_key(
    cache: BuildCache, ds: Dataset, m: store.Manifest, store_dir: Path | None = None
) -> str:
    """A version's cache entry: its register entry and manifest, and for a dataset joined to the
    place spine, the spine layers it reads."""
    if ds.enrich:
        from .spine import spine_versions

        if store_dir is None:
            raise ValueError(f"{ds.slug}: a spine-joined version's key needs the store")
        return cache.key(repr(ds), m.to_json(), spine_versions(ds.enrich, store_dir), "version")
    return cache.key(repr(ds), m.to_json(), "version")


def cache_keys(cache: BuildCache, ds: Dataset, store_dir: Path) -> set[str]:
    """Every entry a build of this dataset can use: its versions, their diffs and its history."""
    if not ds.publishable:
        return set()
    keys = [version_key(cache, ds, m, store_dir) for m in store.manifests(store_dir, ds.slug)]
    diffs = {cache.key(a, b, "diff") for a, b in zip(keys, keys[1:], strict=False)}
    return {*keys, *diffs, *([cache.key(*keys, "history")] if keys else [])}


def _from_cache(ds: Dataset, m: store.Manifest, hit: dict, vdir: Path) -> VersionOut:
    return VersionOut(
        m,
        hit["rows"],
        hit["files"],
        hit["unknown_columns"],
        hit["suppressed_cells"],
        hit["partitions"],
        hit["first"],
        tuple(
            sorted(k for k in hit["files"] if k != source_name(ds, m) and not (vdir / k).exists())
        ),
        hit.get("tables", {}),
        query_key(ds.slug, m.version) if ds.kind != "database" else "",
    )


def _meta(vout: VersionOut, writers: dict[str, str]) -> dict:
    return {
        "rows": vout.rows,
        "files": vout.files,
        "unknown_columns": vout.unknown_columns,
        "suppressed_cells": vout.suppressed_cells,
        "partitions": vout.partitions,
        "first": vout.first,
        "tables": vout.tables,
        "writers": writers,
    }


def current(ds: Dataset, hit: dict, now: dict[str, str]) -> bool:
    """Whether a cached version already holds every format the current writers would make."""
    if ds.kind == "database":
        return True
    want = formats_for(hit["rows"], geo_kind(ds))
    seen = hit.get("writers", {})
    return (
        all(seen.get(f) == now[f] for f in want)
        and all(f"data.{f}" in hit["files"] for f in want)
        and not [f for f in seen if f not in want]
    )


def pending(
    cache: BuildCache, ds: Dataset, store_dir: Path, now: dict[str, str] | None = None
) -> int:
    """The source bytes of the versions a build of this dataset would write from their sources
    or grow: those with no cache entry, and table versions a writer has changed since."""
    from .cache import writer_keys

    if not ds.publishable:
        return 0
    now = now or writer_keys()
    n = 0
    for m in store.manifests(store_dir, ds.slug):
        meta = cache.root / version_key(cache, ds, m, store_dir) / "meta.json"
        if not meta.is_file() or not current(ds, json.loads(meta.read_text("utf-8")), now):
            n += max(m.bytes, 1)
    return n


def take_built(outs: list[DatasetOut], out: Path, root: Path) -> int:
    """Files another job built for versions this build took from the cache, linked in from that
    job's tree so a preview serves them. Returns how many."""
    n = 0
    for o in outs:
        for v in o.versions:
            rel = Path("d") / o.dataset.slug / "v" / v.manifest.version
            took = [f for f in v.absent if (root / rel / f).is_file()]
            for f in took:
                (out / rel / f).parent.mkdir(parents=True, exist_ok=True)
                _link_or_copy(str(root / rel / f), str(out / rel / f))
            v.absent = tuple(f for f in v.absent if f not in took)
            n += len(took)
            q = v.query
            if q and not (out / q).exists() and (root / q).is_file():
                (out / q).parent.mkdir(parents=True, exist_ok=True)
                _link_or_copy(str(root / q), str(out / q))
                n += 1
    return n


def grow_cached(
    ds: Dataset, m: store.Manifest, hit: dict, vdir: Path, cache: BuildCache, key: str
) -> dict | None:
    """A cached table version brought up to the current writers: a format whose writer the
    entry has not seen, or saw in another form, is written again from the cached Parquet, the
    way the diff reads a version back, and a format no longer made is dropped from the record.
    Returns the entry's new metadata, or None when the entry cannot be grown and the version
    must be built from its source."""
    from .cache import writer_keys

    if ds.kind == "database":
        return hit
    want = formats_for(hit["rows"], geo_kind(ds))
    now = writer_keys()
    seen = hit.get("writers", {})
    changed = [f for f in want if seen.get(f) != now[f]]
    stale = list(changed)
    if current(ds, hit, now):
        return hit
    # The rows come from the Parquet, a layer's shapes included; a changed Parquet writer is a new
    # version.
    out = vdir.parents[3]
    rel = vdir.relative_to(out).as_posix()
    if "parquet" in stale or not published.path(out, f"{rel}/data.parquet").exists():
        return None
    base = version_url(ds.slug, m.version)

    def hdr(rows: int, rel: str) -> dict:
        return prov_header(ds, m, rows, base + rel)

    tbl = _built_table(ds, m, out)
    if m.parquet.get("sort"):
        tbl = _source_order(tbl, cache, key, vdir / "data.parquet")
        if tbl is None:
            return None
    if "csv.gz" in stale and "csv" not in stale and not (vdir / "data.csv").exists():
        stale = ["csv", *stale]  # the gzip reads the CSV, written here and not kept
    for p in [vdir / f"data.{f}" for f in stale]:
        p.unlink(missing_ok=True)
    write_formats(tbl, [f for f in formats_for(hit["rows"], geo_kind(ds)) if f in stale], hdr, vdir)
    # A format no longer made is dropped from the record, unless the build is limited to a
    # subset by --formats: the files are still published, and a limited build never shrinks
    # an entry a full build will grow again.
    from . import serialise

    files = {
        k: v
        for k, v in hit["files"].items()
        if not (k.startswith("data.") and k[5:] in WRITERS)
        or k[5:] in want
        or serialise.LIMIT is not None
    }
    for f in stale:
        files[f"data.{f}"] = _size(vdir / f"data.{f}")
    if "csv" in stale and "csv" not in changed:
        (vdir / "data.csv").unlink()  # written only to make the gzip; the published one stands
    writers = {f: now[f] for f in want}
    if serialise.LIMIT is not None:
        writers = {**seen, **writers}  # a limited build forgets no writer it did not run
    meta = {**hit, "files": dict(sorted(files.items())), "writers": writers}
    if "ndjson" in stale:
        meta["first"] = _first_row(vdir)  # the dataset page shows it
    cache.put(key, meta, vdir, kept, _order_file(tbl, vdir / "data.parquet"))
    cache.grown += len(stale)
    return meta


def _cached_version(
    ds: Dataset, m: store.Manifest, store_dir: Path, out: Path, cache: BuildCache | None, key: str
) -> tuple[Table | None, VersionOut]:
    """A version from the cache when its entry exists, without touching the source bytes, else
    built from them. A cached version keeps only the files kept() names."""
    vdir = out / "d" / ds.slug / "v" / m.version
    if cache is not None:
        hit = cache.get(key, vdir)
        if hit is not None:
            hit = grow_cached(ds, m, hit, vdir, cache, key)
        if hit is not None:
            return None, _from_cache(ds, m, hit, vdir)
    store.verify(store_dir, m)
    src = store.source_path(store_dir, m)
    if ds.kind == "database":
        # Read in place: a database release is gigabytes, and never held in memory whole.
        tbl, vout = build_database_version(ds, m, src, out)
    else:
        tbl, vout = build_version(ds, m, src.read_bytes(), out, store_dir)
    if cache is not None:
        from .cache import writer_keys

        now = writer_keys()
        writers = (
            {}
            if ds.kind == "database"
            else {f: now[f] for f in formats_for(vout.rows, geo_kind(ds))}
        )
        order = _order_file(tbl, vdir / "data.parquet") if tbl is not None else {}
        cache.put(key, _meta(vout, writers), vdir, kept, order)
    return tbl, vout


# A sorted version's source order, beside its cache entry, so a format that keeps the publisher's
# order can be written from the sorted Parquet without building the version again. It records
# the Parquet it belongs to, and is used only on that file.
ORDER = "order.parquet"


def _order_file(tbl: Table, parquet: Path) -> dict[str, bytes]:
    """The cache entry's ORDER file for a sorted version, by name, or none."""
    lay = tbl.manifest.parquet
    if not lay.get("sort") or not parquet.is_file():
        return {}
    perm = order_of(tbl, lay["sort"], lay["key"])
    meta = {"parquet_sha256": sha256(parquet), "parquet_bytes": str(parquet.stat().st_size)}
    t = pa.table({"source_row": perm}).replace_schema_metadata(meta)
    sink = pa.BufferOutputStream()
    pq.write_table(t, sink, compression="zstd")
    return {ORDER: sink.getvalue().to_pybytes()}


def _source_order(tbl: Table, cache: BuildCache, key: str, parquet: Path) -> Table | None:
    """A table read back from its sorted Parquet, in the publisher's order again, or None when
    the cache entry records no order for that very file, as when the published file is another
    build's, and the version must be built from its source."""
    p = cache.root / key / ORDER
    if not p.is_file() or not parquet.is_file():
        return None
    t = pq.read_table(p)
    meta = t.schema.metadata or {}
    if (
        meta.get(b"parquet_bytes") != str(parquet.stat().st_size).encode()
        or meta.get(b"parquet_sha256") != sha256(parquet).encode()
        or not signature(parquet)
        or t.num_rows != tbl.rows
    ):
        return None
    perm = t.column("source_row").combine_chunks()
    back = pc.sort_indices(perm)
    geometry = tbl.geometry.take(back) if tbl.geometry is not None else None
    lay = tbl.manifest.parquet
    table = tbl.table.take(back)
    return replace(
        tbl,
        table=table,
        geometry=geometry,
        order=(table, (tuple(lay["sort"]), tuple(lay["key"])), perm),
    )


def diff_database(ds: Dataset, a: VersionOut, b: VersionOut) -> dict:
    """Two versions of a database compared table by table, by row count. The tables are not
    read back: a release of a hundred million rows is compared by what each version holds."""
    tables = sorted(set(a.tables) | set(b.tables))
    return {
        "dataset": ds.slug,
        "from": a.manifest.version,
        "to": b.manifest.version,
        "rows_from": a.rows,
        "rows_to": b.rows,
        "key": [],
        "schema": {
            "tables_added": [t for t in tables if t not in a.tables],
            "tables_removed": [t for t in tables if t not in b.tables],
        },
        "tables": {t: {"rows_from": a.tables.get(t), "rows_to": b.tables.get(t)} for t in tables},
        "note": "A database is compared by the row count of each table.",
    }


def _built_table(ds: Dataset, m: store.Manifest, out: Path) -> Table:
    """A built version's rows read back from its Parquet, which holds exactly what normalise made."""
    t = pq.read_table(published.path(out, f"d/{ds.slug}/v/{m.version}/data.parquet"))
    geometry = None
    if geo_kind(ds) in ("polygon", "line"):
        # A layer's Parquet carries its shapes after the fields.
        geometry = t.column("geometry").combine_chunks()
        t = t.drop(["geometry"])
    # The spine layers a joined version read are in its manifest, which the schema names again.
    man = out / "d" / ds.slug / "v" / m.version / "manifest.json"
    places = json.loads(man.read_text(encoding="utf-8")).get("places", [])
    return Table(
        dataset=ds,
        manifest=m,
        table=widen(t.replace_schema_metadata(None)),
        geometry=geometry,
        places=places,
    )


def build_dataset(
    ds: Dataset, store_dir: Path, out: Path, cache: BuildCache | None = None
) -> DatasetOut:
    dout = DatasetOut(ds)
    if not ds.publishable:
        return dout
    prev = None  # (manifest, table or None, cache key)
    keys = []
    for m in store.manifests(store_dir, ds.slug):
        key = version_key(cache, ds, m, store_dir) if cache else ""
        keys.append(key)
        tbl, vout = _cached_version(ds, m, store_dir, out, cache, key)
        dout.versions.append(vout)
        if prev is not None:
            pm, ptbl, pkey = prev
            path = out / "d" / ds.slug / "diff" / f"{pm.version}..{m.version}.json"
            dkey = cache.key(pkey, key, "diff") if cache else ""
            d = cache.get(dkey) if cache else None
            if d is None:
                if ds.kind == "database":
                    d = diff_database(ds, dout.versions[-2], vout)
                else:
                    ptbl = ptbl if ptbl is not None else _built_table(ds, pm, out)
                    tbl = tbl if tbl is not None else _built_table(ds, m, out)
                    d = diff(ptbl, tbl)
                if cache:
                    cache.put(dkey, d)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pretty(d), encoding="utf-8")
            dout.changes.append(
                {k: v for k, v in d.items() if not k.endswith("_keys") and k != "examples"}
                | {"url": f"{dataset_url(ds.slug)}diff/{d['from']}..{d['to']}.json"}
            )
        prev = (m, tbl, key)
    if not dout.versions:
        return dout
    ddir = out / "d" / ds.slug
    (ddir / "versions.json").write_text(
        pretty(
            {
                "dataset": ds.slug,
                "latest": dout.latest.manifest.version,
                "versions": [
                    {
                        "version": v.manifest.version,
                        "as_at": v.manifest.as_at or None,
                        "url": version_url(ds.slug, v.manifest.version),
                        "rows": v.rows,
                        "fields": ds.field_count,
                        **({"tables": v.tables} if ds.kind == "database" else {}),
                        "sha256": v.manifest.sha256,
                        "bytes": v.manifest.bytes,
                        "encoding": v.manifest.encoding,
                        "filename": v.manifest.filename,
                        "fetched_at": v.manifest.fetched_at,
                        "backfilled": v.manifest.backfilled,
                        "tombstone": v.manifest.tombstone,
                    }
                    for v in dout.versions
                ],
            }
        ),
        encoding="utf-8",
    )
    (ddir / "changes.json").write_text(
        pretty({"dataset": ds.slug, "changes": dout.changes}), encoding="utf-8"
    )
    (ddir / "schema.json").write_text(
        (ddir / "v" / dout.latest.manifest.version / "schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (ddir / "datapackage.json").write_text(pretty(_datapackage(dout)), encoding="utf-8")
    _history(dout, out, cache, keys)
    return dout
