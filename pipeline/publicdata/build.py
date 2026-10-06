"""Turn the register and the raw store into dist/. Never touches upstream: a cached version's
Parquet comes back through published, from the tree or R2 that holds it."""

from __future__ import annotations

import datetime
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
    # A table with a period: whether the whole-table files sit beside its parts, and the parts.
    whole: bool = True
    parts: list[dict] = field(default_factory=list)


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
    # A rolling source's or a feed's newest fetch, served at latest/, and every fetch's counts.
    current: VersionOut | None = None
    fetches: list[dict] = field(default_factory=list)

    @property
    def latest(self) -> VersionOut | None:
        return self.versions[-1] if self.versions else None


def dataset_url(slug: str) -> str:
    return f"{SITE}/d/{slug}/"


def version_url(slug: str, version: str) -> str:
    return f"{SITE}/d/{slug}/v/{version}/"


LATEST = "latest"


def where(slug: str, version: str, tree: str = "") -> str:
    """A built tree's path: a dated version's, or with LATEST, a rolling source's or a feed's
    newest fetch, under a folder of its own date. latest/ serves the folder current.json names,
    so a deploy that is half out never mixes two fetches' files."""
    return f"d/{slug}/fetch/{version}" if tree == LATEST else f"d/{slug}/v/{version}"


def tree_url(slug: str, version: str, tree: str = "") -> str:
    return f"{SITE}/d/{slug}/latest/" if tree == LATEST else version_url(slug, version)


def part_dir(slug: str, rec: dict, version: str) -> str:
    """Where a part's file sits in the built tree: latest/'s own, or the version that wrote it."""
    return where(slug, version, LATEST) if rec["tree"] == LATEST else where(slug, rec["tree"])


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
    ds: Dataset,
    m: store.Manifest,
    data: bytes,
    out: Path,
    store_dir: Path | None = None,
    tree: str = "",
    prior: str = "",
    revised: set[str] | None = None,
    read: str = "",
) -> tuple[Table, VersionOut]:
    """One version's files. With the period its manifest records, its parts too: `prior` is the
    snapshot before, whose finished parts this one takes as they are when unchanged, and
    `revised` the periods the change logs since then revised. `read` is a feed's newest read,
    which latest/'s history carries its current rows to."""
    tbl = normalise(ds, m, data)
    if ds.enrich:
        from .spine import enrich

        if store_dir is None:
            raise ValueError(f"{ds.slug}: joining the place spine needs the store")
        tbl = enrich(tbl, store_dir, REGISTER_DIR)
    tbl = _sorted_once(tbl, m.parquet)
    vdir = out / where(ds.slug, m.version, tree)
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    base = tree_url(ds.slug, m.version, tree)

    def hdr(rows: int, rel: str) -> dict:
        return prov_header(ds, m, rows, base + rel)

    fmts = formats_for(tbl.rows, geo_kind(ds))
    if "ndjson" not in fmts:
        raise ValueError("every build writes data.ndjson, which the dataset page reads back")
    split = _parts(ds, m, tbl, hdr, vdir, out, tree or m.version, prior, store_dir, revised, read)
    whole = split.get("whole", True)
    if whole:
        write_formats(tbl, fmts, hdr, vdir)
        # latest/ is no version, so the query API never reads a copy of it.
        query = "" if tree else _query_copy(tbl, hdr(tbl.rows, "data.parquet"), vdir, out)
        partitions = write_partitions(tbl, hdr, vdir)
        (vdir / "data.csv-metadata.json").write_text(
            pretty(csvw_metadata(tbl, hdr(tbl.rows, "data.csv-metadata.json"))), encoding="utf-8"
        )
    else:
        from .parts import write_duckdb

        write_duckdb(tbl, split["parts"], hdr(tbl.rows, "data.duckdb"), vdir / "data.duckdb")
        query = ""
        partitions = {}
    (vdir / "schema.json").write_text(pretty(table_schema(tbl)), encoding="utf-8")
    (vdir / "schema.sql").write_text(schema_sql(tbl, hdr(tbl.rows, "schema.sql")), encoding="utf-8")
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
    man.update(split)
    man["url"] = base
    (vdir / "manifest.json").write_text(pretty(man), encoding="utf-8")
    return tbl, VersionOut(
        m,
        tbl.rows,
        _with_source(_sizes(vdir), ds, m),
        tbl.unknown_columns,
        tbl.suppressed_cells,
        partitions,
        _first_row(vdir) if whole else _first_of(tbl),
        query=query,
        whole=whole,
        parts=split.get("parts", []),
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


def _parts(
    ds: Dataset,
    m: store.Manifest,
    tbl: Table,
    hdr,
    vdir: Path,
    out: Path,
    tree: str,
    prior: str,
    store_dir: Path | None,
    revised: set[str] | None = None,
    read: str = "",
) -> dict:
    """A period table's parts, and a feed's history in parts of its own, for the manifest."""
    from . import parts
    from .periods import of_manifest

    per = of_manifest(m)
    if per is None:
        return {}
    before = {}
    if prior:
        p = out / where(ds.slug, prior) / "manifest.json"
        before = json.loads(p.read_text(encoding="utf-8"))
    recs, flagged = parts.write(
        tbl, tbl.table, per, hdr, vdir, tree, before.get("parts", []), revised=revised
    )
    split = {
        "period": m.period,
        "whole": parts.whole(recs, tbl.rows),
        "parts": recs,
        "revised": flagged,
    }
    if m.history:
        if store_dir is None:
            raise ValueError(f"{ds.slug}: a feed's history is read from the store")
        path = store.history_path(store_dir, m)
        if not path.exists() or store.sha256_file(path) != m.history["sha256"]:
            raise FileNotFoundError(f"{path} is missing or changed; run `publicdata store pull`")
        seen = pq.read_table(path).replace_schema_metadata(None)
        if read and read > m.version:
            from .updates import LAST_SEEN

            # Rows the newest fetch held were still there at every read since.
            col = seen.column(LAST_SEEN)
            was = pa.scalar(datetime.date.fromisoformat(m.version), pa.date32())
            moved = pc.if_else(
                pc.equal(col, was), pa.scalar(datetime.date.fromisoformat(read), pa.date32()), col
            )
            seen = seen.set_column(seen.schema.get_field_index(LAST_SEEN), LAST_SEEN, moved)
        # A history part changes whenever a current row's last_seen moves, which is no revision.
        hrecs, _ = parts.write(
            tbl,
            seen,
            per,
            hdr,
            vdir,
            tree,
            before.get("history", {}).get("parts", []),
            "history",
            revised=set(),
        )
        (vdir / "history").mkdir(exist_ok=True)
        (vdir / "history" / "schema.json").write_text(
            pretty(history_schema(tbl, seen)), encoding="utf-8"
        )
        split["history"] = {
            **m.history,
            "schema": "history/schema.json",
            "parts": hrecs,
            **({"read": read} if read else {}),
        }
    return split


def history_schema(tbl: Table, seen: pa.Table) -> dict:
    """A feed history's Table Schema: the table's own fields it keeps, then first_seen and
    last_seen, marked as computed by this site the way a joined column is."""
    from .updates import FIRST_SEEN, LAST_SEEN

    base = table_schema(tbl)
    fields = [f for f in base["fields"] if f["name"] in seen.column_names]
    for name, words in (
        (FIRST_SEEN, "The date publicdata.au first read this row, in this state, in the feed."),
        (LAST_SEEN, "The date publicdata.au last read this row, in this state, in the feed."),
    ):
        fields.append(
            {
                "name": name,
                "type": "date",
                "title": name,
                "description": words,
                "publicdata:derived": {"method": "observed by publicdata.au"},
            }
        )
    # A key holds one row per state, so a state is its key and the day it was first read.
    key = {"primaryKey": [*base["primaryKey"], FIRST_SEEN]} if base.get("primaryKey") else {}
    return {**base, "fields": fields, **key}


def _first_of(tbl: Table) -> str:
    """The first record as data.ndjson would hold it, for a version written as parts alone."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        one = replace(tbl, table=tbl.table.slice(0, 1))
        WRITERS["ndjson"](one, {}, Path(tmp) / "data.ndjson", Path(tmp))
        return _first_row(Path(tmp))


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
            names = (
                kept
                if v.whole
                else (
                    "manifest.json",
                    *sorted(
                        f for f in v.files if f.startswith("parts/") and f.endswith(".parquet")
                    ),
                )
            )
            # A feed's history parts this version wrote, whole or not.
            names = (
                *names,
                *sorted(f for f in v.files if f.startswith("history/") and f.endswith(".parquet")),
            )
            for name in names:
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
    elif not v.whole:
        from .parts import url

        resources.append(
            {
                "name": "duckdb",
                "path": base + "data.duckdb",
                "format": "duckdb",
                "mediatype": MEDIA["duckdb"],
                "bytes": v.files.get("data.duckdb"),
                "schema": f"{base}schema.json",
                "description": "Attach it read-only over HTTPS; records() reads every part's Parquet. The size is to one significant figure.",
            }
        )
        for r in v.parts:
            resources.append(
                {
                    "name": f"part-{r['period']}",
                    "path": url(ds.slug, r, "parquet"),
                    "format": "parquet",
                    "mediatype": MEDIA["parquet"],
                    "bytes": r["files"]["parquet"]["bytes"],
                    "schema": f"{base}schema.json",
                    "publicdata:rows": r["rows"],
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
    if ds.kind != "database" and v.whole:
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
    cache: BuildCache,
    ds: Dataset,
    m: store.Manifest,
    store_dir: Path | None = None,
    prior: str = "",
) -> str:
    """A version's cache entry: its register entry and manifest, and for a dataset joined to the
    place spine, the spine layers it reads. A version split by period also takes the snapshot
    before's key and the revisions logged since, which decide its parts, so a cold build and a
    warm one never lay its parts out differently."""
    split = ()
    if m.period:
        # A release flags revisions by digest (None); a rolling source or a feed by its change
        # logs, whose set may be empty. The two never share a key.
        revised = revised_since(ds, store_dir, m)
        split = (prior, "digest" if revised is None else "logs:" + ",".join(sorted(revised)))
    if ds.enrich:
        from .spine import spine_versions

        if store_dir is None:
            raise ValueError(f"{ds.slug}: a spine-joined version's key needs the store")
        return cache.key(
            repr(ds), m.to_json(), spine_versions(ds.enrich, store_dir), *split, "version"
        )
    return cache.key(repr(ds), m.to_json(), *split, "version")


def version_keys(
    cache: BuildCache, ds: Dataset, store_dir: Path
) -> list[tuple[store.Manifest, str]]:
    """Every snapshot with its key, each key taking the one before it."""
    out: list[tuple[store.Manifest, str]] = []
    for m in store.manifests(store_dir, ds.slug):
        out.append((m, version_key(cache, ds, m, store_dir, out[-1][1] if out else "")))
    return out


def revised_since(ds: Dataset, store_dir: Path | None, m: store.Manifest) -> set[str] | None:
    """The periods the change logs revised after the snapshot before m, up to m itself. None for
    a version fetched as a release, which keeps no change log; the class is the one m records, so
    a later change of class leaves the version as it was."""
    if not m.update or m.update == "release" or store_dir is None:
        return None
    all_ = store.manifests(store_dir, ds.slug, fetches=True)
    snaps = [x.version for x in all_ if x.snapshot and x.version < m.version]
    after = snaps[-1] if snaps else ""
    out: set[str] = set()
    for f in all_:
        if after < f.version <= m.version:
            p = store.change_log(store_dir, f)
            if p.exists():
                out |= set(json.loads(p.read_text(encoding="utf-8")).get("revised", ()))
    return out


def newest_fetch(ds: Dataset, store_dir: Path) -> store.Manifest | None:
    """The fetch latest/ serves: a rolling source's or a feed's newest, snapshot or not."""
    if ds.update == "release" or not ds.publishable:
        return None
    ms = store.manifests(store_dir, ds.slug, fetches=True)
    return ms[-1] if ms else None


def latest_key(cache: BuildCache, ds: Dataset, m: store.Manifest, store_dir: Path) -> str:
    """latest/: the newest fetch, the snapshot whose finished parts it points at, and a feed's
    newest read, which moves its history's last_seen."""
    snaps = version_keys(cache, ds, store_dir)
    prior = snaps[-1][1] if snaps else ""
    read = store.read_since(store_dir, ds.slug, m.version) if ds.update == "feed" else ""
    return cache.key(version_key(cache, ds, m, store_dir, prior), read, LATEST)


def cache_keys(cache: BuildCache, ds: Dataset, store_dir: Path) -> set[str]:
    """Every entry a build of this dataset can use: its versions, their diffs and its history,
    and a rolling source's latest/."""
    if not ds.publishable:
        return set()
    keys = [k for _, k in version_keys(cache, ds, store_dir)]
    diffs = {cache.key(a, b, "diff") for a, b in zip(keys, keys[1:], strict=False)}
    now = newest_fetch(ds, store_dir)
    latest = {latest_key(cache, ds, now, store_dir)} if now else set()
    return {*keys, *diffs, *([cache.key(*keys, "history")] if keys else []), *latest}


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
        query=hit.get("query", query_key(ds.slug, m.version) if ds.kind != "database" else ""),
        whole=hit.get("whole", True),
        parts=hit.get("parts", []),
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
        # A version with no query copy, latest/ or one written as parts alone, says so.
        **({} if vout.query else {"query": ""}),
        **({} if vout.whole else {"whole": False}),
        **({"parts": vout.parts} if vout.parts else {}),
    }


def current(ds: Dataset, hit: dict, now: dict[str, str]) -> bool:
    """Whether a cached version already holds every format the current writers would make."""
    if ds.kind == "database" or hit.get("whole", True) is False:
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
    trees = version_keys(cache, ds, store_dir)
    if fetched := newest_fetch(ds, store_dir):
        trees.append((fetched, latest_key(cache, ds, fetched, store_dir)))
    for m, key in trees:
        meta = cache.root / key / "meta.json"
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
    ds: Dataset,
    m: store.Manifest,
    hit: dict,
    vdir: Path,
    cache: BuildCache,
    key: str,
    tree: str = "",
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
    rel = where(ds.slug, m.version, tree)
    out = vdir.parents[len(Path(rel).parts) - 1]
    if "parquet" in stale or not published.path(out, f"{rel}/data.parquet").exists():
        return None
    # A period table's parts are written by the Parquet and CSV writers with the version.
    if m.period and {"csv", "csv.gz"} & set(stale):
        return None
    base = tree_url(ds.slug, m.version, tree)

    def hdr(rows: int, rel: str) -> dict:
        return prov_header(ds, m, rows, base + rel)

    tbl = _built_table(ds, m, out, tree)
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
    cache.put(key, meta, vdir, keeper(tree))
    if m.parquet.get("sort"):
        _keep_order(cache, key, tbl, vdir / "data.parquet")
    cache.grown += len(stale)
    return meta


def keeper(tree: str):
    """What a cache entry keeps: a dated version only the files kept() names, and latest/ every
    file, since latest/ is served from the deploy that builds it and never left out."""
    return (lambda rel: True) if tree else kept


def _cached_version(
    ds: Dataset,
    m: store.Manifest,
    store_dir: Path,
    out: Path,
    cache: BuildCache | None,
    key: str,
    tree: str = "",
    prior: str = "",
    read: str = "",
) -> tuple[Table | None, VersionOut]:
    """A version from the cache when its entry exists, without touching the source bytes, else
    built from them. A cached version keeps only the files keeper() names."""
    vdir = out / where(ds.slug, m.version, tree)
    if cache is not None:
        hit = cache.get(key, vdir)
        if hit is not None:
            hit = grow_cached(ds, m, hit, vdir, cache, key, tree)
        if hit is not None:
            return None, _from_cache(ds, m, hit, vdir)
    store.verify(store_dir, m)
    src = store.source_path(store_dir, m)
    if ds.kind == "database":
        # Read in place: a database release is gigabytes, and never held in memory whole.
        tbl, vout = build_database_version(ds, m, src, out)
    else:
        tbl, vout = build_version(
            ds,
            m,
            src.read_bytes(),
            out,
            store_dir,
            tree,
            prior,
            revised_since(ds, store_dir, m),
            read,
        )
    if cache is not None:
        from .cache import writer_keys

        now = writer_keys()
        writers = (
            {}
            if ds.kind == "database"
            else {f: now[f] for f in formats_for(vout.rows, geo_kind(ds))}
        )
        cache.put(key, _meta(vout, writers), vdir, keeper(tree))
        if tbl is not None and m.parquet.get("sort") and (vdir / "data.parquet").is_file():
            _keep_order(cache, key, tbl, vdir / "data.parquet")
    return tbl, vout


# A sorted version's source order, beside its cache entry, so a format that keeps the publisher's
# order can be written from the sorted Parquet without building the version again. It records
# the Parquet it belongs to, and is used only on that file.
ORDER = "order.parquet"


def _keep_order(cache: BuildCache, key: str, tbl: Table, parquet: Path) -> None:
    lay = tbl.manifest.parquet
    perm = order_of(tbl, lay["sort"], lay["key"])
    meta = {"parquet_sha256": sha256(parquet), "parquet_bytes": str(parquet.stat().st_size)}
    t = pa.table({"source_row": perm}).replace_schema_metadata(meta)
    pq.write_table(t, cache.root / key / ORDER, compression="zstd")


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


def _built_table(ds: Dataset, m: store.Manifest, out: Path, tree: str = "") -> Table:
    """A built version's rows read back from its Parquet, which holds exactly what normalise made."""
    rel = where(ds.slug, m.version, tree)
    built = json.loads((out / rel / "manifest.json").read_text(encoding="utf-8"))
    if built.get("whole", True):
        t = pq.read_table(published.path(out, f"{rel}/data.parquet"))
    else:
        # A version written as parts alone is read back part by part, wherever each was written;
        # a part written before the column types narrowed is widened to meet the rest.
        t = pa.concat_tables(
            [
                pq.read_table(
                    published.path(
                        out, f"{part_dir(ds.slug, r, m.version)}/{r['files']['parquet']['path']}"
                    )
                ).replace_schema_metadata(None)
                for r in built["parts"]
            ],
            promote_options="permissive",
        )
    geometry = None
    if geo_kind(ds) in ("polygon", "line"):
        # A layer's Parquet carries its shapes after the fields.
        geometry = t.column("geometry").combine_chunks()
        t = t.drop(["geometry"])
    # The spine layers a joined version read are in its manifest, which the schema names again.
    places = built.get("places", [])
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
    chain = (
        version_keys(cache, ds, store_dir)
        if cache
        else [(m, "") for m in store.manifests(store_dir, ds.slug)]
    )
    for m, key in chain:
        keys.append(key)
        tbl, vout = _cached_version(
            ds, m, store_dir, out, cache, key, prior=prev[0].version if prev else ""
        )
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
                **(
                    {"update": ds.update, "changes": f"{dataset_url(ds.slug)}changes/index.json"}
                    if ds.update != "release"
                    else {}
                ),
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
                        **({"cut": v.manifest.cut} if v.manifest.cut else {}),
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
    if ds.update != "release":
        _rolling(dout, store_dir, out, cache)
    return dout


def _rolling(dout: DatasetOut, store_dir: Path, out: Path, cache: BuildCache | None) -> None:
    """A rolling source's or a feed's change log for every fetch, under changes/, and its newest
    fetch built whole at latest/."""
    from .updates import counts

    ds = dout.dataset
    ddir = out / "d" / ds.slug
    for f in store.manifests(store_dir, ds.slug, fetches=True):
        p = store.change_log(store_dir, f)
        if not p.exists():
            continue
        log = json.loads(p.read_text(encoding="utf-8"))
        rel = f"changes/{f.version}.json"
        (ddir / "changes").mkdir(exist_ok=True)
        (ddir / rel).write_text(pretty(log), encoding="utf-8")
        dout.fetches.append(
            {
                "fetch": f.version,
                "fetched_at": f.fetched_at,
                "snapshot": f.cut or None,
                "url": f"{dataset_url(ds.slug)}{rel}",
                **counts(log),
            }
        )
    m = newest_fetch(ds, store_dir)
    key = latest_key(cache, ds, m, store_dir) if cache else ""
    read = store.read_since(store_dir, ds.slug, m.version) if ds.update == "feed" else ""
    dout.current = _cached_version(
        ds, m, store_dir, out, cache, key, LATEST, dout.latest.manifest.version, read
    )[1]
    (ddir / "changes").mkdir(exist_ok=True)
    (ddir / "changes" / "index.json").write_text(
        pretty(
            {
                "dataset": ds.slug,
                "update": ds.update,
                "latest": f"{dataset_url(ds.slug)}latest/",
                "latest_fetch": m.version,
                **({"read": read} if read else {}),
                "snapshot": dout.latest.manifest.version,
                "fetches": dout.fetches,
            }
        ),
        encoding="utf-8",
    )
