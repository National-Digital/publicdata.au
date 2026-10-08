"""SQL that loads dataset versions into D1 for the query API.

Each loaded version is one table built from the version's own data.parquet, read as its
data.sqlite holds it, so the API answers from the same typed rows as every file. The latest version of each live dataset is loaded; a
dataset keeps at most KEEP versions in the database, and every version stays available as files.
`_versions` records what is loaded, with the field list the API validates queries against.

A load fills a table no reader can see, named for its content, and registers it in `_versions`
only once its row count and indexes are checked in the same statement; the version it replaces
keeps serving until then and is dropped after. `_loads` holds what is pending: when it was first
seen, how far a load got and how often it failed. D1 bills rows written, so each deploy loads up
to BUDGET of them, oldest pending first, and the rest wait for the next deploy.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import math
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow.parquet as pq

from .records import connect
from .serialise import SQLITE_TYPES, dumps
from .serialise.profile import sha256, signature

KEEP = 2
MAX_STATEMENT = 90_000  # D1 allows 100 KB per statement
MAX_ROW = 2_000_000  # and 2 MB per row
# D1 answers SQLITE_NOMEM for one INSERT carrying about 20,000 values or more (1,529 rows of 13
# columns loaded, 1,565 failed), whatever its size in bytes, so an INSERT carries half that.
MAX_VALUES = 10_000
# D1 runs out of memory when one import holds more than a few MB of rows (20 MB failed, 5 MB
# loaded 75,000 rows in 7 s), so a version loads in parts of this size.
PART_BYTES = 5_000_000
# A version whose data.csv is larger stays files-only: loading runs at about 45 MB of SQL a
# minute and the database holds 10 GB in all. The CSV is on every version, where SQLite is not.
MAX_CSV = 500_000_000
# Versions load side by side, each writing its own table and its own _versions row, and the parts
# of one version always run in order.
WORKERS = 4
# D1 runs one import per database at a time and refuses a second while the first is in flight,
# so the parts of every version and the checks after them go through this lock.
IMPORT_LOCK = threading.Lock()
# Rows written one deploy may plan, counting each index entry and the full-text index.
BUDGET = 10_000_000
# A dataset whose loads failed in this many deploys, whatever the versions, waits for a forced
# retry; a load that succeeds clears the count.
MAX_FAILURES = 3
# A dataset skipped, or waiting longer than this, is listed on every deploy.
STALE_DAYS = 7
# A load table's name: the version's table name and a digest. The sweep drops only these.
OWNED = re.compile(r"v_[a-z0-9_]+_[0-9a-f]{10}")
# Tries per part within one deploy, and per check against D1.
TRIES = 2
ASKS = 3
ASK_DELAY = 5.0
# Rows the full-text index writes per row of its table, near enough to plan by.
FTS_WRITES = 2
# Part of every load table's name, so a change to how rows are written loads afresh.
LOADER = "2"
REGISTRY = """CREATE TABLE IF NOT EXISTS _versions (
  slug TEXT NOT NULL,
  version TEXT NOT NULL,
  tbl TEXT NOT NULL,
  fields TEXT NOT NULL,
  rows INTEGER NOT NULL,
  attribution TEXT NOT NULL,
  header TEXT NOT NULL,
  PRIMARY KEY (slug, version)
);"""
# The row order each loaded version was taken in (serialise.profile.signature), kept apart from
# _versions so the registry the API reads keeps its columns. A version loaded in another order
# than its Parquet's is loaded again, so rowid agrees with the Parquet and the console.
ORDERS = """CREATE TABLE IF NOT EXISTS _orders (
  slug TEXT NOT NULL,
  version TEXT NOT NULL,
  ord TEXT NOT NULL,
  PRIMARY KEY (slug, version)
);"""
# One row per dataset with a version waiting to load: the table it fills, the parts of it in
# place and the rows they hold, the deploys that failed it, and since when the dataset waits.
LOADS = """CREATE TABLE IF NOT EXISTS _loads (
  slug TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  tbl TEXT NOT NULL,
  part INTEGER NOT NULL,
  rows INTEGER NOT NULL,
  attempts INTEGER NOT NULL,
  error TEXT,
  since TEXT NOT NULL,
  tried TEXT
);"""


def table_name(slug: str, version: str) -> str:
    return "v_" + re.sub(r"[^a-z0-9]", "_", slug) + "_" + version.replace("-", "")


def literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v) if math.isfinite(v) else "NULL"
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def version_sql(
    sqlite_path: Path,
    slug: str,
    version: str,
    index_fields: tuple[str, ...],
    tbl: str | None = None,
    fts: tuple[str, ...] = (),
):
    """Yields the statements that create and fill one table from a SQLite file.

    The file is shaped as a version's data.sqlite, as the catalogue and served indexes are.
    """
    for _, stmt, _ in _sqlite_stmts(sqlite_path, slug, version, index_fields, tbl, fts):
        yield stmt


def _sqlite_stmts(sqlite_path, slug, version, index_fields, tbl=None, fts=()):
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        cols = [(c[1], c[2]) for c in src.execute("PRAGMA table_info(records)").fetchall()]
        header = dict(src.execute("SELECT key, value FROM publicdata").fetchall())
        rows = src.execute(f"SELECT {', '.join(_q(c) for c, _ in cols)} FROM records")
        yield from _table_sql(
            slug, version, index_fields, cols, header, _fields(src), rows, tbl, fts
        )
    finally:
        src.close()


def parquet_version_sql(
    parquet: Path, ds, version: str, index_fields: tuple[str, ...], tbl: str | None = None
):
    """Yields the statements that create and fill one dataset version's table from its Parquet.

    The table is typed as its data.sqlite and filled in the Parquet's row order, which is the
    publisher's unless the version was written under a sort.
    """
    for _, stmt, _ in _parquet_stmts(parquet, ds, version, index_fields, tbl):
        yield stmt


def _parquet_stmts(parquet, ds, version, index_fields, tbl=None):
    cols = parquet_columns(parquet, ds)
    header = {
        k: v if isinstance(v, str) else dumps(v)
        for k, v in json.loads(pq.read_schema(parquet).metadata[b"publicdata"]).items()
    }
    with connect(parquet, [c for c, _ in cols]) as src:
        src.execute(f"SELECT {', '.join(_q(c) for c, _ in cols)} FROM records")
        rows = (r for batch in iter(lambda: src.fetchmany(10_000), []) for r in batch)
        yield from _table_sql(
            ds.slug, version, index_fields, cols, header, built_fields(ds, parquet), rows, tbl
        )


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def parquet_columns(parquet: Path, ds) -> list[tuple[str, str]]:
    """The records table's columns and SQLite types, as the SQLite writer declares them."""
    have = set(pq.read_schema(parquet).names)
    cols = [(f.name, SQLITE_TYPES[f.type]) for f in ds.fields if f.name in have]
    return cols + ([("suppressed", "TEXT")] if "suppressed" in have else [])


def _table_sql(slug, version, index_fields, cols, header, fields, rows, tbl=None, fts=()):
    """Yields (kind, statement, rows it inserts).

    Kinds "create", "insert" and "update" fill the table; "index", "fts" and "register" finish it
    and can run again. The registration names the table and holds only if the table has every row
    and every index.
    """
    tbl = tbl or table_name(slug, version)
    names = [c for c, _ in cols]
    if fts:
        yield "create", f'DROP TABLE IF EXISTS "{tbl}_fts";', 0
    yield "create", f'DROP TABLE IF EXISTS "{tbl}";', 0
    yield (
        "create",
        f'CREATE TABLE "{tbl}" ({", ".join(f"{chr(34)}{c}{chr(34)} {t}" for c, t in cols)});',
        0,
    )
    head = f'INSERT INTO "{tbl}" ({", ".join(f"{chr(34)}{n}{chr(34)}" for n in names)}) VALUES '
    # D1's limit is in bytes, and a name or an address in another script takes several per letter.
    batch, size, n = [], len(head.encode()), 0
    wide: dict[int, int] = {}
    max_rows = max(1, MAX_VALUES // max(1, len(names)))
    for row in rows:
        values = "(" + ",".join(literal(v) for v in row) + ")"
        width = len(values.encode())
        n += 1
        if len(head.encode()) + width + 1 > MAX_STATEMENT:
            if batch:
                yield "insert", head + ",".join(batch) + ";", len(batch)
                batch, size = [], len(head.encode())
            yield from _wide_row(tbl, head, names, row, n, wide)
            continue
        if batch and (size + width + 2 > MAX_STATEMENT or len(batch) >= max_rows):
            yield "insert", head + ",".join(batch) + ";", len(batch)
            batch, size = [], len(head.encode())
        batch.append(values)
        size += width + 1
    if batch:
        yield "insert", head + ",".join(batch) + ";", len(batch)
    indexes = [f for f in dict.fromkeys(index_fields) if f in names]
    for f in indexes:
        yield "index", f'CREATE INDEX IF NOT EXISTS "{tbl}_{f}" ON "{tbl}" ("{f}");', 0
    if fts:
        text = ", ".join(fts)
        yield "fts", f'DROP TABLE IF EXISTS "{tbl}_fts";', 0
        yield (
            "fts",
            (
                f"CREATE VIRTUAL TABLE \"{tbl}_fts\" USING fts5({text}, content='{tbl}', "
                "content_rowid='rowid', tokenize='porter unicode61 remove_diacritics 2');"
            ),
            0,
        )
        for a in range(0, n, FTS_ROWS):
            yield (
                "fts",
                (
                    f'INSERT INTO "{tbl}_fts" (rowid, {text}) SELECT rowid, {text} FROM "{tbl}" '
                    f"WHERE rowid > {a} AND rowid <= {a + FTS_ROWS};"
                ),
                0,
            )
    # The provenance header every file of this version carries, parsed back to one object.
    prov = {}
    for k, v in header.items():
        try:
            prov[k] = json.loads(v)
        except ValueError:
            prov[k] = v
    values = ", ".join(
        literal(x)
        for x in (
            slug,
            version,
            tbl,
            json.dumps(fields),
            n,
            prov.get("attribution", ""),
            json.dumps(prov, ensure_ascii=False, sort_keys=True),
        )
    )
    holds = [
        f'(SELECT COUNT(*) FROM "{tbl}") = {n}',
        (
            "(SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND tbl_name = "
            f"{literal(tbl)}) = {len(indexes)}"
        ),
    ]
    if fts:
        holds.append(f'(SELECT COUNT(*) FROM "{tbl}_fts_docsize") = {n}')
    if wide:
        # The row count cannot see an append that failed, so the version registers only when
        # every value built a piece at a time holds all its bytes.
        sizes = " + ".join(
            f"""CASE WHEN typeof("{c}") IN ('text', 'blob') THEN length(CAST("{c}" AS BLOB)) ELSE 0 END"""
            for c in names
        )
        holds.append(
            f'(SELECT sum({sizes}) FROM "{tbl}" WHERE rowid IN '
            f"({','.join(map(str, wide))})) = {sum(wide.values())}"
        )
    register = f"INSERT OR REPLACE INTO _versions SELECT {values} WHERE {' AND '.join(holds)};"
    if len(register.encode()) > MAX_STATEMENT:
        # Raised once every row is read and before any part is written.
        msg = (
            f"its registration check is {len(register.encode()):,} bytes, over D1's statement limit"
        )
        raise TooWide(msg)
    yield "register", register, 0


class TooWide(ValueError):
    """A row larger than D1 holds; its version stays files-only."""


def _raw_size(v) -> int:
    if isinstance(v, str):
        return len(v.encode())
    if isinstance(v, bytes):
        return len(v)
    return 0


def _wide_row(tbl: str, head: str, names: list[str], row, rowid: int, wide: dict[int, int]):
    """A row too long for one statement, inserted in pieces.

    It goes in with its longest text and blob values empty, and each of those is then appended a
    piece at a time. The table is new and fills in order, so the row is the `rowid`th. Records the
    row's bytes in `wide` for the check at registration.
    """
    if sum(_raw_size(v) for v in row) > MAX_ROW:
        msg = f"row {rowid} is larger than D1 holds"
        raise TooWide(msg)
    lits = [literal(v) for v in row]
    later = []
    for i in sorted(range(len(row)), key=lambda i: -len(lits[i].encode())):
        if len(head.encode()) + sum(len(x.encode()) for x in lits) + len(lits) + 2 <= MAX_STATEMENT:
            break
        if not isinstance(row[i], (str, bytes)):
            msg = f"row {rowid} is too long for one statement"
            raise TooWide(msg)
        lits[i] = "''" if isinstance(row[i], str) else "X''"
        later.append(i)
    yield "insert", head + "(" + ",".join(lits) + ");", 1
    for i in later:
        col, v = names[i], row[i]
        stem = f'UPDATE "{tbl}" SET "{col}" = '
        tail = f" WHERE rowid = {rowid};"
        room = MAX_STATEMENT - len((stem + tail).encode()) - 40
        if isinstance(v, bytes):
            step = room // 2 - 2
            for a in range(0, len(v), step):
                yield (
                    "update",
                    f"{stem}CAST(\"{col}\" || X'{v[a : a + step].hex()}' AS BLOB){tail}",
                    0,
                )
            continue
        a = 0
        while a < len(v):
            k = room
            while len(literal(v[a : a + k]).encode()) > room:
                k = max(1, k * room // len(literal(v[a : a + k]).encode()) - 1)
            yield "update", f'{stem}"{col}" || {literal(v[a : a + k])}{tail}', 0
            a += k
    wide[rowid] = sum(_raw_size(v) for v in row)


def queryable(ds, csv_bytes: int | None) -> bool:
    """Whether the query API serves this version, by the size of its data.csv.

    The build and the loader both ask here.
    """
    return bool(ds.query and csv_bytes and csv_bytes <= MAX_CSV)


def _fields(src: sqlite3.Connection) -> list[dict]:
    """The fields of the records table, as _versions lists them for the API."""
    names = [c[1] for c in src.execute("PRAGMA table_info(records)").fetchall()]
    return [
        {"name": n, "type": t}
        for n, t in src.execute("SELECT name, type FROM fields").fetchall()
        if n in names
    ]


def built_fields(ds, parquet: Path) -> list[dict]:
    """The field list a version registers, as its data.sqlite's fields table names them."""
    have = set(pq.read_schema(parquet).names)
    fields = [{"name": f.name, "type": f.type} for f in ds.fields if f.name in have]
    return fields + ([{"name": "suppressed", "type": "array"}] if "suppressed" in have else [])


def _csv_bytes(roots: list[Path], slug: str, version: str) -> int | None:
    """The size of a version's data.csv, as its dataset's data package lists it."""
    want = f"/d/{slug}/v/{version}/data.csv"
    for r in roots:
        p = r / "d" / slug / "datapackage.json"
        if p.exists():
            for res in json.loads(p.read_text(encoding="utf-8")).get("resources", []):
                if str(res.get("path", "")).endswith(want):
                    return res.get("bytes")
    return None


def rows_written(text: str) -> int:
    """A count of rows as a dispatch input gives it: 10000000, 10_000,000, 10M, 500k or 1G."""
    m = re.fullmatch(r"\s*(\d[\d_,]*)\s*([kKmMgG]?)\s*", text or "")
    if not m:
        msg = f"{text!r} is not a count of rows, such as 10000000 or 10M"
        raise ValueError(msg)
    n = int(m[1].replace("_", "").replace(",", ""))
    return n * {"": 1, "k": 10**3, "m": 10**6, "g": 10**9}[m[2].lower()]


def load_table(slug: str, version: str, *content) -> str:
    """The table one load fills: the version's name and a digest of what goes in it.

    That way a load never writes into a table the API reads, and a load resumed later is of the
    same rows.
    """
    h = hashlib.sha256(LOADER.encode())
    for c in content:
        h.update(b"\0" + (c if isinstance(c, bytes) else json.dumps(c).encode()))
    return f"{table_name(slug, version)}_{h.hexdigest()[:10]}"


def _sqlite_digest(path: Path, table: str = "records") -> bytes:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for q in (
            f"SELECT * FROM {table} ORDER BY rowid",
            "SELECT * FROM fields ORDER BY rowid",
            "SELECT * FROM publicdata ORDER BY key",
        ):
            for row in src.execute(q):
                h.update(repr(row).encode())
            h.update(b"\0")
        return h.digest()
    finally:
        src.close()


def write_loads(
    roots: list[Path],
    datasets,
    loaded: dict[str, list[str]],
    out: Path,
    stamp: str = "",
    loaded_fields: dict[tuple[str, str], str] | None = None,
    loaded_orders: dict[tuple[str, str], str] | None = None,
) -> list[Path]:
    """SQL files for each live dataset whose latest version is not loaded yet.

    They come in parts that run in name order, with a manifest the loader plans from. `roots` are
    the built trees to look for data.parquet in, such as dist and the tree split off for R2. A
    loaded version whose fields differ from the built one, as when a column is joined in, or whose
    rows were taken in another order than its Parquet's, is loaded again into a table of its own.
    """
    out.mkdir(parents=True, exist_ok=True)
    written = []
    latest_json = next((r / "latest.json" for r in roots if (r / "latest.json").exists()), None)
    latest = json.loads(latest_json.read_text(encoding="utf-8")) if latest_json else {}
    for ds in datasets:
        version = latest.get(ds.slug)
        if not version:
            continue
        rel = Path("d") / ds.slug / "v" / version / "data.parquet"
        src = next((r / rel for r in roots if (r / rel).exists()), None)
        if version in loaded.get(ds.slug, []):
            had = (loaded_fields or {}).get((ds.slug, version))
            ord_ = (loaded_orders or {}).get((ds.slug, version), "")
            if (
                had is None
                or src is None
                or (json.loads(had) == built_fields(ds, src) and ord_ == signature(src))
            ):
                continue
        if src is None or not queryable(ds, _csv_bytes(roots, ds.slug, version)):
            continue
        index = (*ds.key, *ds.partition_by)
        sig = signature(src)
        tbl = load_table(
            ds.slug,
            version,
            sha256(src),
            parquet_columns(src, ds),
            built_fields(ds, src),
            list(index),
            sig,
        )
        try:
            stmts = list(_parquet_stmts(src, ds, version, index, tbl))
        except TooWide as e:
            print(f"d1: {ds.slug}@{version} stays files-only: {e}")  # noqa: T201 - the deploy log
            continue
        # The order is recorded only once the version is registered under this table.
        order = (
            f"INSERT OR REPLACE INTO _orders SELECT {literal(ds.slug)}, {literal(version)}, "
            f"{literal(sig)} WHERE EXISTS (SELECT 1 FROM _versions WHERE slug = "
            f"{literal(ds.slug)} AND version = {literal(version)} AND tbl = {literal(tbl)});"
        )
        written += _write_load(out, ds.slug, version, tbl, stmts, stamp, KEEP, after=[order])
    return written


def _write_load(out, slug, version, tbl, stmts, stamp, keep, fts=False, after=()) -> list[Path]:
    """Writes one load's parts and its manifest.

    Every part but the last only adds rows, and `cum` records the rows the table holds after each,
    which is what a resumed load checks. The last part builds the indexes and registers the
    version, and can run again.
    """
    body: list[list[str]] = []
    cum: list[int] = []
    finish = [REGISTRY, ORDERS]
    rows = indexes = updates = size = 0
    for kind, stmt, n in stmts:
        if kind in ("index", "fts", "register"):
            finish.append(stmt)
            indexes += kind == "index"
            continue
        if not body or size + len(stmt) > PART_BYTES:
            if body:
                cum.append(rows)
            body.append([])
            size = 0
        body[-1].append(stmt)
        size += len(stmt) + 1
        rows += n
        updates += kind == "update"
    cum.append(rows)
    finish += after
    if keep == 1:
        # Readers take the highest version, and _served's are hashes, so the others go in the same
        # part, once this one is registered.
        finish.append(
            f"DELETE FROM _versions WHERE slug = {literal(slug)} AND version != {literal(version)} "
            f"AND EXISTS (SELECT 1 FROM _versions WHERE slug = {literal(slug)} AND version = "
            f"{literal(version)} AND tbl = {literal(tbl)});"
        )
    key = f"{slug}@{version}"
    paths = []
    for i, part in enumerate([*body, finish], 1):
        path = out / f"{key}.part{i:03d}.sql"
        with path.open("w", encoding="utf-8", newline="\n") as f:
            if stamp:
                # D1 re-processes a file it has seen before instead of taking it afresh, and
                # that path fails; a per-run comment makes every part a new file.
                f.write(f"-- load {stamp}\n")
            f.writelines(s + "\n" for s in part)
        paths.append(path)
    manifest = {
        "slug": slug,
        "version": version,
        "tbl": tbl,
        "rows": rows,
        "parts": [p.name for p in paths],
        "cum": cum,
        "indexes": indexes,
        "fts": fts,
        "updates": updates,
        "keep": keep,
    }
    (out / f"{key}.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return paths


# The catalogue search index: every listed record on the portals, one table per harvest, with a
# full-text index over title, summary and publisher. It is registered under a name no dataset
# slug can take, so the rows API never answers from it.
CATALOGUE = "_catalogue"
CATALOGUE_FIELDS = (
    "id",
    "title",
    "summary",
    "publisher",
    "publisher_path",
    "jur",
    "portal",
    "host",
    "name",
    "url",
    "licence",
    "formats",
    "modified",
    "state",
    "vote",
    "note",
)
FTS_ROWS = 20_000


def catalogue_sqlite(path: Path, rows: list[dict], version: str) -> None:
    """The index as a SQLite file shaped like a version's data.sqlite, so it loads the same way."""
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE records ({', '.join(f'{f} TEXT' for f in CATALOGUE_FIELDS)})")
    db.execute("CREATE TABLE fields (name TEXT, type TEXT)")
    db.execute("CREATE TABLE publicdata (key TEXT, value TEXT)")
    db.executemany("INSERT INTO fields VALUES (?, 'string')", [(f,) for f in CATALOGUE_FIELDS])
    db.executemany(
        "INSERT INTO publicdata VALUES (?, ?)",
        [
            ("attribution", "Each record carries the licence its portal states."),
            ("catalogue_read", version),
        ],
    )
    db.executemany(
        f"INSERT INTO records VALUES ({', '.join('?' for _ in CATALOGUE_FIELDS)})",
        [tuple(r[f] for f in CATALOGUE_FIELDS) for r in rows],
    )
    db.commit()
    db.close()


def catalogue_loads(sqlite_path: Path, loaded: list[str], out: Path, stamp: str = "") -> list[Path]:
    """Load parts for the catalogue index when this harvest is not loaded yet.

    A harvest is a new version only when the portals' lists changed, so an unchanged catalogue
    never loads again. One index is kept.
    """
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    version = dict(src.execute("SELECT key, value FROM publicdata").fetchall())["catalogue_read"]
    src.close()
    if not version or version in loaded:
        return []
    out.mkdir(parents=True, exist_ok=True)
    tbl = load_table(CATALOGUE, version, _sqlite_digest(sqlite_path))
    try:
        stmts = list(
            _sqlite_stmts(
                sqlite_path,
                CATALOGUE,
                version,
                ("id", "portal", "host", "name", "url", "vote"),
                tbl,
                ("title", "summary", "publisher"),
            )
        )
    except TooWide as e:
        print(f"d1: {CATALOGUE}@{version} not loaded: {e}")  # noqa: T201 - the deploy log
        return []
    return _write_load(out, CATALOGUE, version, tbl, stmts, stamp, 1, fts=True)


# The datasets served here, for search_datasets: one small table with a full-text index, kept in
# the catalogue index file as `served` and loaded under its own name whenever its rows change.
SERVED = "_served"
SERVED_FIELDS = (
    "slug",
    "title",
    "summary",
    "publisher",
    "jur",
    "licence",
    "page",
    "latest",
    "keywords",
    "fields",
)


def served_table(path: Path, rows: list[dict]) -> str:
    """Adds the served datasets to the catalogue index file.

    Returns the version, which is a hash of the rows so a changed register loads again without
    waiting for a harvest.
    """
    body = json.dumps([[r[f] for f in SERVED_FIELDS] for r in rows], ensure_ascii=False)
    version = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    db = sqlite3.connect(path)
    db.execute("DROP TABLE IF EXISTS served")
    db.execute(f"CREATE TABLE served ({', '.join(f'{f} TEXT' for f in SERVED_FIELDS)})")
    db.executemany(
        f"INSERT INTO served VALUES ({', '.join('?' for _ in SERVED_FIELDS)})",
        [tuple(r[f] for f in SERVED_FIELDS) for r in rows],
    )
    db.execute("INSERT INTO publicdata VALUES ('served_version', ?)", (version,))
    db.commit()
    db.close()
    return version


def served_loads(sqlite_path: Path, loaded: list[str], out: Path, stamp: str = "") -> list[Path]:
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    meta = dict(src.execute("SELECT key, value FROM publicdata").fetchall())
    version = meta.get("served_version")
    if not version or version in loaded:
        src.close()
        return []
    rows = src.execute(f"SELECT {', '.join(SERVED_FIELDS)} FROM served").fetchall()
    src.close()
    tmp = out / f".{SERVED}.sqlite"
    out.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.execute(f"CREATE TABLE records ({', '.join(f'{f} TEXT' for f in SERVED_FIELDS)})")
    db.execute("CREATE TABLE fields (name TEXT, type TEXT)")
    db.execute("CREATE TABLE publicdata (key TEXT, value TEXT)")
    db.executemany("INSERT INTO fields VALUES (?, 'string')", [(f,) for f in SERVED_FIELDS])
    db.execute("INSERT INTO publicdata VALUES ('attribution', 'Each dataset carries its own.')")
    db.executemany(f"INSERT INTO records VALUES ({', '.join('?' for _ in SERVED_FIELDS)})", rows)
    db.commit()
    db.close()
    try:
        tbl = load_table(SERVED, version, _sqlite_digest(tmp))
        text = ("title", "summary", "publisher", "keywords", "fields")
        stmts = list(_sqlite_stmts(tmp, SERVED, version, ("slug",), tbl, text))
    except TooWide as e:
        print(f"d1: {SERVED}@{version} not loaded: {e}")  # noqa: T201 - the deploy log
        return []
    finally:
        tmp.unlink()
    return _write_load(out, SERVED, version, tbl, stmts, stamp, 1, fts=True)


class Wrangler:
    """Runs D1 commands through wrangler. Tests pass a stand-in."""

    def __init__(self, database: str = "publicdata"):
        self.database = database

    def file(self, path: Path) -> bool:
        cmd = [
            "npx",
            "--yes",
            "wrangler@4",
            "d1",
            "execute",
            self.database,
            "--remote",
            "--yes",
            "--file",
            str(path),
        ]
        return subprocess.run(cmd, check=False).returncode == 0

    def query(self, sql: str) -> list[dict]:
        cmd = [
            "npx",
            "--yes",
            "wrangler@4",
            "d1",
            "execute",
            self.database,
            "--remote",
            "--yes",
            "--json",
            "--command",
            sql,
        ]
        r = subprocess.run(cmd, check=False, capture_output=True, text=True)
        try:
            out = json.loads(r.stdout or "{}")
        except ValueError:
            out = {}
        if r.returncode != 0 or isinstance(out, dict):
            msg = f"D1 query failed: {sql[:80]}: {r.stdout[:300]} {r.stderr[:300]}"
            raise RuntimeError(msg)
        return [row for part in out for row in part.get("results", [])]


class Unknown(RuntimeError):
    """D1 did not answer a check, so what a table holds is not known. Never read as absent."""


def _absent(e: Exception) -> bool:
    return "no such table" in str(e)


def _ask(db, sql: str) -> list[dict]:
    """A query, asked again when D1 fails to answer. A missing table is an answer."""
    for i in range(ASKS):
        try:
            return db.query(sql)
        except RuntimeError as e:
            if _absent(e) or i == ASKS - 1:
                raise
            time.sleep(ASK_DELAY)
    msg = "unreachable"
    raise AssertionError(msg)


def _registry(db) -> dict[tuple[str, str], dict]:
    try:
        rows = _ask(db, "SELECT slug, version, tbl, rows FROM _versions")
    except RuntimeError as e:
        if _absent(e):
            return {}
        raise
    return {(r["slug"], r["version"]): {"tbl": r["tbl"], "rows": int(r["rows"])} for r in rows}


def registered(db) -> dict[tuple[str, str], int]:
    return {k: v["rows"] for k, v in _registry(db).items()}


def _count(db, tbl: str) -> int | None:
    """The rows a table holds, None when it does not exist. Raises Unknown when D1 does not say."""
    try:
        return int(_ask(db, f'SELECT COUNT(*) AS n FROM "{tbl}"')[0]["n"])
    except RuntimeError as e:
        if _absent(e):
            return None
        raise Unknown(str(e)) from e
    except (LookupError, TypeError, ValueError) as e:
        msg = f"no count for {tbl}: {e}"
        raise Unknown(msg) from e


def holds(db, tbl: str, expected: int) -> bool:
    """One COUNT per table, since D1 caps compound SELECTs at five terms.

    Raises Unknown rather than answer False when D1 cannot be asked.
    """
    return _count(db, tbl) == expected


def restamp(path: Path, attempt: int) -> None:
    text = path.read_text(encoding="utf-8")
    first, rest = text.split("\n", 1) if text.startswith("-- load ") else ("-- load", text)
    path.write_text(f"{first} retry {attempt}\n{rest}", encoding="utf-8")


@dataclasses.dataclass
class Job:
    """One version's load, as its manifest describes it, and where it stands."""

    key: str
    slug: str
    version: str
    tbl: str
    rows: int
    parts: list[Path]
    cum: list[int]
    indexes: int
    fts: bool
    updates: int
    keep: int
    start: int = 0  # body parts already in the table, as `_loads` records them
    attempts: int = 0
    since: str = ""
    outcome: str = ""
    note: str = ""
    first: bool = False
    charged: int = 0

    @property
    def body(self) -> list[Path]:
        return self.parts[:-1]

    def finishing(self) -> int:
        return self.rows * self.indexes + (FTS_WRITES * self.rows if self.fts else 0)

    @property
    def resumable(self) -> bool:
        # An appended piece of a wide row leaves no trace in the row count, so such a version
        # always loads from its first part.
        return self.updates == 0

    def planned(self) -> int:
        """Rows written: each row, each index entry, the full-text index and wide-row pieces."""
        done = self.cum[self.start - 1] if self.start else 0
        return self.rows - done + self.finishing() + self.updates


def _jobs(folder: Path) -> list[Job]:
    jobs = []
    for m in sorted(folder.glob("*.json")):
        d = json.loads(m.read_text(encoding="utf-8"))
        jobs.append(
            Job(
                key=m.name[: -len(".json")],
                slug=d["slug"],
                version=d["version"],
                tbl=d["tbl"],
                rows=d["rows"],
                parts=[folder / p for p in d["parts"]],
                cum=d["cum"],
                indexes=d["indexes"],
                fts=d["fts"],
                updates=d["updates"],
                keep=d["keep"],
            )
        )
    return jobs


def plan(jobs: list[Job], state: dict[str, dict], budget: int, retry: set[str], now: str):
    """Orders the loads and splits them into this deploy's and later ones'.

    A load that got part way goes first, then the dataset that has waited longest, then the
    smaller. Loads are taken while they fit the budget, and the first in line is taken whatever
    its size, so a version larger than the budget loads alone in some deploy and none waits for
    good. A dataset whose loads failed in MAX_FAILURES deploys, of any versions, is skipped until
    a load of it succeeds or `retry` names it; a failure also restarts its wait, so it goes behind
    the others.
    """
    queue = []
    for j in jobs:
        row = state.get(j.slug)
        j.since = row["since"] if row else now
        j.attempts = int(row["attempts"]) if row else 0
        if row and row["version"] == j.version:
            part, done = int(row["part"]), int(row["rows"])
            if (
                row["tbl"] == j.tbl
                and j.resumable
                and 0 < part <= len(j.cum)
                and j.cum[part - 1] == done
            ):
                j.start = part
        if j.attempts >= MAX_FAILURES and not ({"all", j.slug} & retry):
            j.outcome = "skipped"
            j.note = f"failed in {j.attempts} deploys: {(row or {}).get('error') or ''}"[:300]
            continue
        queue.append(j)
    queue.sort(key=lambda j: (j.start == 0, j.since, j.planned(), j.key))
    take, wait, used = [], [], 0
    for j in queue:
        if not take or used + j.planned() <= budget:
            take.append(j)
            used += j.planned()
        else:
            j.outcome = "deferred"
            wait.append(j)
    return take, wait


def _upsert(rows: list[tuple], since: bool = False) -> str:
    return (
        "INSERT INTO _loads (slug, version, tbl, part, rows, attempts, error, since, tried) VALUES "
        + ",".join("(" + ", ".join(literal(v) for v in r) + ")" for r in rows)
        + " ON CONFLICT(slug) DO UPDATE SET version = excluded.version, tbl = excluded.tbl, "
        "part = excluded.part, rows = excluded.rows, attempts = excluded.attempts, "
        "error = excluded.error, tried = excluded.tried"
        + (", since = excluded.since;" if since else ";")
    )


def _drops(tbl: str) -> list[str]:
    return [f'DROP TABLE IF EXISTS "{tbl}_fts";', f'DROP TABLE IF EXISTS "{tbl}";']


def _note_pending(db, jobs: list[Job], state: dict[str, dict], served: set[str], log) -> None:
    """Records each pending load the registry of loads lacks or holds for another table.

    It keeps the time its dataset began to wait, and drops a table an earlier load left part
    filled. A row whose dataset is no longer pending goes, with any table it was filling.
    """
    rows, sql = [], []
    pending = {j.slug for j in jobs}
    for j in jobs:
        row = state.get(j.slug)
        if j.outcome == "skipped" or (row and (row["version"], row["tbl"]) == (j.version, j.tbl)):
            continue
        if row and row["tbl"] != j.tbl and row["tbl"] not in served:
            sql += _drops(row["tbl"])
        rows.append(
            (
                j.slug,
                j.version,
                j.tbl,
                0,
                0,
                j.attempts,
                row["error"] if row else None,
                j.since,
                row["tried"] if row else None,
            )
        )
    for slug, row in state.items():
        if slug not in pending:
            if row["tbl"] not in served:
                sql += _drops(row["tbl"])
            sql.append(f"DELETE FROM _loads WHERE slug = {literal(slug)};")
    for i in range(0, len(rows), 50):
        sql.append(_upsert(rows[i : i + 50]))
    for s in sql:
        try:
            _ask(db, s)
        except RuntimeError as e:
            log(f"d1 load: could not record pending loads: {e}")
            return


class _Skip(Exception):
    """Nothing was written, and this deploy cannot tell where the load stands."""


class _Defer(Exception):
    """The deploy's budget is spent; nothing of this load was written."""


class _Budget:
    """Rows written so far in this deploy.

    A load reserves its plan before it writes, and each part run again, or a resume that has to
    start over, is charged on top. Once retries have used it up, loads not yet started wait for
    the next deploy; the first in line always runs.
    """

    def __init__(self, cap: int):
        self.cap, self.spent, self.lock = cap, 0, threading.Lock()

    def reserve(self, j: Job, need: int) -> None:
        with self.lock:
            if not j.first and self.spent + need > self.cap:
                msg = f"{self.spent:,} of the {self.cap:,} rows written already spent"
                raise _Defer(msg)
            self.spent += need
            j.charged += need

    def charge(self, j: Job, rows: int) -> None:
        with self.lock:
            self.spent += rows
            j.charged += rows


class _Failed(Exception):
    def __init__(self, done: int, why: str):
        super().__init__(why)
        self.done = done


def _fill(db, j: Job, budget: _Budget, log) -> None:
    """Runs the parts that add rows.

    It starts from where the last load stopped if the table still holds exactly the rows that load
    recorded. Raises _Failed with the parts known to be in place.
    """
    start = j.start
    if start:
        try:
            with IMPORT_LOCK:
                n = _count(db, j.tbl)
        except Unknown as e:
            raise _Skip(str(e)) from e
        if n != j.cum[start - 1]:
            log(
                f"d1 load: {j.key} holds {n} rows, not the {j.cum[start - 1]} recorded after "
                f"part {start}; loading it from part 1"
            )
            start = 0
            budget.reserve(j, j.cum[j.start - 1])
        else:
            log(f"d1 load: {j.key} resumes after part {start} of {len(j.body)}")
    for i in range(start, len(j.body)):
        before = j.cum[i - 1] if i else 0
        for t in range(1, TRIES + 1):
            if t > 1:
                budget.charge(j, j.cum[i] - before)
            with IMPORT_LOCK:
                if db.file(j.body[i]):
                    break
                # An import can report an error after it has applied; the count tells.
                n = _count(db, j.tbl)
            if n == j.cum[i]:
                break
            if not j.resumable:
                raise _Failed(0, f"part {i + 1} reported an error")
            if n != before and not (i == 0 and n is None):
                raise _Failed(0, f"part {i + 1} left {n} rows, not {before} or {j.cum[i]}")
            if t == TRIES:
                raise _Failed(i, f"part {i + 1} did not apply in {TRIES} tries")
            restamp(j.body[i], t)


def _finish(db, j: Job, budget: _Budget, log) -> None:
    """Builds the indexes and registers the version, which holds only if the table is whole."""
    with IMPORT_LOCK:
        n = _count(db, j.tbl)
    if n != j.rows:
        raise _Failed(0, f"holds {n} rows before its indexes, not {j.rows}")
    for t in range(1, TRIES + 1):
        if t > 1:
            budget.charge(j, j.finishing())
        with IMPORT_LOCK:
            reported = db.file(j.parts[-1])
            try:
                reg = _ask(
                    db,
                    f"SELECT tbl, rows FROM _versions WHERE slug = {literal(j.slug)} "
                    f"AND version = {literal(j.version)}",
                )
            except RuntimeError as e:
                raise Unknown(str(e)) from e
            ok = bool(reg) and reg[0]["tbl"] == j.tbl and int(reg[0]["rows"]) == j.rows
            ok = ok and holds(db, j.tbl, j.rows)
        log(
            f"d1 load: {j.key} finish try {t}: {'reported ok' if reported else 'reported an error'}"
            f", {'registered' if ok else 'not registered'}"
        )
        if ok:
            return
        restamp(j.parts[-1], t)
    raise _Failed(len(j.body), "its indexes or registration did not complete")


def _sweep(db, keep: set[str], log) -> None:
    """Drops the load tables, and their full-text indexes, that nothing names.

    A table goes when no registration, pending load or load of this deploy names it; it is what a
    confirmation D1 did not answer, or a cleanup that failed, left behind. Only names shaped as
    load_table makes them are touched.
    """
    try:
        names = {
            r["name"]
            for r in _ask(
                db, "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'v%'"
            )
        }
    except RuntimeError as e:
        log(f"d1 load: no sweep this deploy: {e}")
        return
    for name in sorted(names):
        base = name.removesuffix("_fts")
        if not OWNED.fullmatch(base) or base in keep:
            continue
        try:
            _ask(db, f'DROP TABLE IF EXISTS "{name}"')
            log(f"d1 load: dropped {name}, which nothing names")
        except RuntimeError as e:
            log(f"d1 load: could not drop {name}: {e}")
            return


def _clean(db, j: Job, reg: dict[tuple[str, str], dict], folder: Path, log) -> None:
    """Unregisters the versions beyond the ones kept, after the new table is registered.

    It then drops the tables no registration names any more, the one this load replaced among
    them.
    """
    others = sorted((v for s, v in reg if s == j.slug and v != j.version), reverse=True)
    # Newer versions are kept first, so a re-run of an older deploy never drops one.
    kept = [v for v in others if v > j.version][: j.keep - 1]
    kept += [v for v in others if v < j.version][: j.keep - 1 - len(kept)]
    gone = [v for v in others if v not in kept]
    names = {reg[(j.slug, v)]["tbl"] for v in kept} | {j.tbl}
    stmts = []
    for v in gone:
        stmts += [
            f"DELETE FROM _versions WHERE slug = {literal(j.slug)} AND version = {literal(v)};",
            f"DELETE FROM _orders WHERE slug = {literal(j.slug)} AND version = {literal(v)};",
        ]
    old = [reg[(j.slug, v)]["tbl"] for v in gone]
    if (j.slug, j.version) in reg:
        old.append(reg[(j.slug, j.version)]["tbl"])
    for t in dict.fromkeys(old):
        if t not in names:
            stmts += _drops(t)
    stmts.append(f"DELETE FROM _loads WHERE slug = {literal(j.slug)} AND tbl = {literal(j.tbl)};")
    path = folder / f"{j.key}.clean.sql"
    path.write_text("\n".join(stmts) + "\n", encoding="utf-8")
    with IMPORT_LOCK:
        if not db.file(path):
            log(f"d1 load: {j.key} is loaded; dropping what it replaced reported an error")


def _record_failure(db, j: Job, done: int, why: str, now: str, log) -> None:
    j.attempts += 1
    rows = j.cum[done - 1] if done else 0
    try:
        with IMPORT_LOCK:
            _ask(
                db,
                _upsert(
                    [(j.slug, j.version, j.tbl, done, rows, j.attempts, why[:500], now, now)],
                    since=True,
                ),
            )
    except RuntimeError as e:
        log(f"d1 load: {j.key} failure could not be recorded: {e}")


def _run(db, j: Job, reg, served: set[str], folder: Path, now: str, budget: _Budget, log) -> None:
    try:
        if j.tbl in served:
            # Registered by a deploy that could not confirm it; only the finish part runs again.
            if j.fts:
                raise _Failed(0, f"{j.tbl} is registered; its full-text index is not rebuilt")  # noqa: TRY301 - recorded below with every failure
            budget.reserve(j, j.finishing())
        else:
            budget.reserve(j, j.planned())
            _fill(db, j, budget, log)
        _finish(db, j, budget, log)
    except _Defer as e:
        log(f"d1 load: {j.key} waits for the next deploy: {e}")
        j.outcome, j.note = "deferred", str(e)
        return
    except _Skip as e:
        log(f"d1 load: {j.key} skipped this deploy, D1 did not answer: {e}")
        j.outcome, j.note = "unchecked", str(e)[:300]
        return
    except _Failed as e:
        log(f"d1 load: {j.key} failed: {e}")
        _record_failure(db, j, e.done, str(e), now, log)
        j.outcome, j.note = "failed", str(e)
        return
    except Unknown as e:
        # Counted as a failure with no progress, so a check D1 never answers cannot reload a
        # version on every deploy. Nothing is unregistered: a registered version stays served.
        log(f"d1 load: {j.key} not checked, D1 did not answer: {e}")
        _record_failure(db, j, 0, f"unchecked: {e}", now, log)
        j.outcome, j.note = "unchecked", str(e)[:300]
        return
    except (RuntimeError, OSError, ValueError, KeyError, IndexError) as e:
        log(f"d1 load: {j.key} failed: {e}")
        _record_failure(db, j, 0, str(e), now, log)
        j.outcome, j.note = "failed", str(e)[:300]
        return
    j.outcome = "resumed" if j.start else "loaded"
    try:
        _clean(db, j, reg, folder, log)
    except (RuntimeError, OSError) as e:
        log(f"d1 load: {j.key} is loaded; dropping what it replaced failed: {e}")


def _stale(j: Job, now: str) -> bool:
    try:
        since = datetime.datetime.fromisoformat(j.since)
        at = datetime.datetime.fromisoformat(now)
    except ValueError:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=datetime.UTC)
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.UTC)
    return at - since > datetime.timedelta(days=STALE_DAYS)


def _summary(
    path: Path | None, jobs: list[Job], budget: int, log, spent: int = 0, now: str = ""
) -> None:
    order = {"loaded": 0, "resumed": 0, "failed": 1, "unchecked": 2, "deferred": 3, "skipped": 4}
    jobs = sorted(jobs, key=lambda j: (order.get(j.outcome, 5), j.key))
    for j in jobs:
        if j.outcome == "skipped":
            log(
                f"::warning title=D1 load skipped::{j.key} {j.note}. It loads again once "
                f"a deploy is dispatched with d1_retry: {j.slug}"
            )
        elif j.outcome in ("failed", "unchecked"):
            left = MAX_FAILURES - j.attempts
            more = (
                f"; skipped from now on after {left} more" if left > 0 else "; skipped from now on"
            )
            log(f"::warning title=D1 load {j.outcome}::{j.key} {j.note[:200]}{more}")
    stale = [j for j in jobs if j.outcome == "skipped" or (now and _stale(j, now))]
    for j in stale:
        if j.outcome != "skipped":
            log(
                f"::warning title=D1 load waiting::{j.key} has waited since {j.since[:10]}; "
                "the API answers from the version before it"
            )
    if path is None:
        return
    lines = [
        "## D1 load",
        "",
        f"Budget {budget:,} rows written, a soft cap; {spent:,} charged by the loads that ran.",
        "",
    ]
    if stale:
        lines += [
            (
                f"Skipped, or waiting over {STALE_DAYS} days; the API answers from the version "
                "before each:"
            ),
            "",
            *(
                f"- {j.key}: {'skipped, ' + j.note if j.outcome == 'skipped' else 'waiting'}"
                f" since {j.since[:10]}".replace("\n", " ")[:300]
                for j in stale
            ),
            "",
        ]
    lines += [
        "| Version | Outcome | Rows | Planned rows written | Waiting since | Note |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for j in jobs:
        what = f"resumed after part {j.start}" if j.outcome == "resumed" else j.outcome
        note = j.note.replace("|", "/").replace("\n", " ")[:160]
        lines.append(
            f"| {j.key} | {what} | {j.rows:,} | {j.planned():,} | {j.since[:16]} | {note} |"
        )
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        log(f"d1 load: no step summary: {e}")


def load(
    folder: Path,
    db,
    log=print,
    workers: int = WORKERS,
    budget: int = BUDGET,
    retry: set[str] | None = None,
    summary: Path | None = None,
    now: str | None = None,
) -> int:
    """Loads this deploy's share of the pending versions and leaves the rest to later deploys.

    A part that reports an error is counted: one that applied is kept, one that did not is run
    again, anything else fails the version. A failed version keeps its progress and the version
    before it keeps serving. Up to `workers` versions load at once. Returns the number of
    versions that failed in this deploy.
    """
    jobs = _jobs(folder)
    now = now or datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    for ddl in (REGISTRY, ORDERS, LOADS):
        _ask(db, ddl)
    reg = _registry(db)
    served = {r["tbl"] for r in reg.values()}
    state = {r["slug"]: r for r in _ask(db, "SELECT * FROM _loads")}
    take, wait = plan(jobs, state, budget, retry or set(), now)
    _note_pending(db, jobs, state, served, log)
    _sweep(db, served | {j.tbl for j in jobs} | {r["tbl"] for r in state.values()}, log)
    if take:
        take[0].first = True
    spent = _Budget(budget)
    log(
        f"d1 load: {len(take)} to load ({sum(j.planned() for j in take):,} rows written), "
        f"{len(wait)} deferred, {sum(j.outcome == 'skipped' for j in jobs)} skipped"
    )

    def one(j: Job) -> None:
        _run(db, j, reg, served, folder, now, spent, log)

    if workers <= 1:
        for j in take:
            one(j)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, take))
    _summary(summary, jobs, budget, log, spent.spent, now)
    return sum(j.outcome == "failed" for j in jobs)
