"""SQL that loads dataset versions into D1 for the query API.

Each loaded version is one table built from the version's own data.parquet, read as its
data.sqlite holds it, so the API answers from the same typed rows as every file. The latest version of each live dataset is loaded; a
dataset keeps at most KEEP versions in the database, and every version stays available as files.
`_versions` records what is loaded, with the field list the API validates queries against.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow.parquet as pq

from .serialise import SQLITE_TYPES, dumps
from .serialise.profile import signature

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


def version_sql(sqlite_path: Path, slug: str, version: str, index_fields: tuple[str, ...]):
    """Yields the statements that create and fill one table from a SQLite file shaped as a
    version's data.sqlite, as the catalogue and served indexes are."""
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        cols = [(c[1], c[2]) for c in src.execute("PRAGMA table_info(records)").fetchall()]
        header = dict(src.execute("SELECT key, value FROM publicdata").fetchall())
        rows = src.execute(f"SELECT {', '.join(_q(c) for c, _ in cols)} FROM records")
        yield from _table_sql(slug, version, index_fields, cols, header, _fields(src), rows)
    finally:
        src.close()


def parquet_version_sql(parquet: Path, ds, version: str, index_fields: tuple[str, ...]):
    """Yields the statements that create and fill one dataset version's table from its Parquet,
    typed as its data.sqlite and in the Parquet's row order, which is the publisher's unless
    the version was written under a sort."""
    from .records import connect

    cols = parquet_columns(parquet, ds)
    header = {
        k: v if isinstance(v, str) else dumps(v)
        for k, v in json.loads(pq.read_schema(parquet).metadata[b"publicdata"]).items()
    }
    with connect(parquet, [c for c, _ in cols]) as src:
        src.execute(f"SELECT {', '.join(_q(c) for c, _ in cols)} FROM records")
        rows = (r for batch in iter(lambda: src.fetchmany(10_000), []) for r in batch)
        yield from _table_sql(
            ds.slug, version, index_fields, cols, header, built_fields(ds, parquet), rows
        )


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def parquet_columns(parquet: Path, ds) -> list[tuple[str, str]]:
    """The records table's columns and SQLite types, as the SQLite writer declares them."""
    have = set(pq.read_schema(parquet).names)
    cols = [(f.name, SQLITE_TYPES[f.type]) for f in ds.fields if f.name in have]
    return cols + ([("suppressed", "TEXT")] if "suppressed" in have else [])


def _table_sql(slug, version, index_fields, cols, header, fields, rows):
    tbl = table_name(slug, version)
    names = [c for c, _ in cols]
    yield f'DROP TABLE IF EXISTS "{tbl}";'
    yield f'CREATE TABLE "{tbl}" ({", ".join(f"{chr(34)}{c}{chr(34)} {t}" for c, t in cols)});'
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
                yield head + ",".join(batch) + ";"
                batch, size = [], len(head.encode())
            yield from _wide_row(tbl, head, names, row, n, wide)
            continue
        if batch and (size + width + 2 > MAX_STATEMENT or len(batch) >= max_rows):
            yield head + ",".join(batch) + ";"
            batch, size = [], len(head.encode())
        batch.append(values)
        size += width + 1
    if batch:
        yield head + ",".join(batch) + ";"
    for f in dict.fromkeys(index_fields):
        if f in names:
            yield f'CREATE INDEX "{tbl}_{f}" ON "{tbl}" ("{f}");'
    # The provenance header every file of this version carries, parsed back to one object.
    prov = {}
    for k, v in header.items():
        try:
            prov[k] = json.loads(v)
        except ValueError:
            prov[k] = v
    register = (
        "INSERT OR REPLACE INTO _versions "
        + ("VALUES (" if not wide else "SELECT ")
        + ", ".join(
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
    )
    if wide:
        # The row count cannot see an append that failed, so the version registers only when
        # every value built a piece at a time holds all its bytes.
        sizes = " + ".join(
            f"""CASE WHEN typeof("{c}") IN ('text', 'blob') THEN length(CAST("{c}" AS BLOB)) ELSE 0 END"""
            for c in names
        )
        register += (
            f' WHERE (SELECT sum({sizes}) FROM "{tbl}" WHERE rowid IN '
            f"({','.join(map(str, wide))})) = {sum(wide.values())}"
        )
    yield register + (");" if not wide else ";")


class TooWide(ValueError):
    """A row larger than D1 holds; its version stays files-only."""


def _raw_size(v) -> int:
    if isinstance(v, str):
        return len(v.encode())
    if isinstance(v, bytes):
        return len(v)
    return 0


def _wide_row(tbl: str, head: str, names: list[str], row, rowid: int, wide: dict[int, int]):
    """A row too long for one statement: it goes in with its longest text and blob values empty,
    and each of those is then appended a piece at a time. The table is new and fills in order, so
    the row is the `rowid`th. Records the row's bytes in `wide` for the check at registration."""
    if sum(_raw_size(v) for v in row) > MAX_ROW:
        raise TooWide(f"row {rowid} is larger than D1 holds")
    lits = [literal(v) for v in row]
    later = []
    for i in sorted(range(len(row)), key=lambda i: -len(lits[i].encode())):
        if len(head.encode()) + sum(len(x.encode()) for x in lits) + len(lits) + 2 <= MAX_STATEMENT:
            break
        if not isinstance(row[i], (str, bytes)):
            raise TooWide(f"row {rowid} is too long for one statement")
        lits[i] = "''" if isinstance(row[i], str) else "X''"
        later.append(i)
    yield head + "(" + ",".join(lits) + ");"
    for i in later:
        col, v = names[i], row[i]
        stem = f'UPDATE "{tbl}" SET "{col}" = '
        tail = f" WHERE rowid = {rowid};"
        room = MAX_STATEMENT - len((stem + tail).encode()) - 40
        if isinstance(v, bytes):
            step = room // 2 - 2
            for a in range(0, len(v), step):
                yield f"{stem}CAST(\"{col}\" || X'{v[a : a + step].hex()}' AS BLOB){tail}"
            continue
        a = 0
        while a < len(v):
            k = room
            while len(literal(v[a : a + k]).encode()) > room:
                k = max(1, k * room // len(literal(v[a : a + k]).encode()) - 1)
            yield f'{stem}"{col}" || {literal(v[a : a + k])}{tail}'
            a += k
    wide[rowid] = sum(_raw_size(v) for v in row)


def prune_sql(slug: str, loaded: list[str], new: str, keep: int = KEEP):
    """Statements that drop the versions beyond the newest `keep`, counting the one being loaded."""
    versions = sorted(set(loaded) | {new}, reverse=True)
    for v in versions[keep:]:
        yield f'DROP TABLE IF EXISTS "{table_name(slug, v)}";'
        yield f"DELETE FROM _versions WHERE slug = {literal(slug)} AND version = {literal(v)};"
        yield f"DELETE FROM _orders WHERE slug = {literal(slug)} AND version = {literal(v)};"


def queryable(ds, csv_bytes: int | None) -> bool:
    """Whether the query API serves this version, by the size of its data.csv. The build and
    the loader both ask here."""
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


def write_loads(
    roots: list[Path],
    datasets,
    loaded: dict[str, list[str]],
    out: Path,
    stamp: str = "",
    loaded_fields: dict[tuple[str, str], str] | None = None,
    loaded_orders: dict[tuple[str, str], str] | None = None,
) -> list[Path]:
    """SQL files for each live dataset whose latest version is not loaded yet, in parts that run
    in name order. The first part recreates the table and the last registers the version, so a
    part that fails leaves a table the API never reads and the next deploy starts that version
    again. `roots` are the built trees to look for data.parquet in, such as dist and the tree
    split off for R2. A loaded version whose fields differ from the built one, as when a column
    is joined in, or whose rows were taken in another order than its Parquet's, is loaded
    again."""
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
        try:
            body = list(parquet_version_sql(src, ds, version, (*ds.key, *ds.partition_by)))
        except TooWide as e:
            print(f"d1: {ds.slug}@{version} stays files-only: {e}")
            continue
        register = body.pop()
        # Registering is the last statement of all, so a version counts as loaded only once every
        # part, the pruning of older versions included, has run.
        order = (
            "INSERT OR REPLACE INTO _orders VALUES "
            f"({literal(ds.slug)}, {literal(version)}, {literal(signature(src))});"
        )
        stmts = [
            REGISTRY,
            ORDERS,
            *body,
            *prune_sql(ds.slug, loaded.get(ds.slug, []), version),
            order,
            register,
        ]
        written += _parts(out, f"{ds.slug}@{version}", stmts, stamp)
    return written


def _parts(out: Path, key: str, stmts, stamp: str) -> list[Path]:
    written = []
    part, size, f = 0, 0, None
    for stmt in stmts:
        if f is None or size + len(stmt) > PART_BYTES:
            if f:
                f.close()
            part += 1
            path = out / f"{key}.part{part:03d}.sql"
            f = path.open("w", encoding="utf-8", newline="\n")
            if stamp:
                # D1 re-processes a file it has seen before instead of taking it afresh, and
                # that path fails; a per-run comment makes every part a new file.
                f.write(f"-- load {stamp}\n")
            written.append(path)
            size = 0
        f.write(stmt + "\n")
        size += len(stmt) + 1
    if f:
        f.close()
    return written


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
    """Load parts for the catalogue index when this harvest is not loaded yet. One index is kept."""
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    version = dict(src.execute("SELECT key, value FROM publicdata").fetchall())["catalogue_read"]
    n = src.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    src.close()
    if not version or version in loaded:
        return []
    out.mkdir(parents=True, exist_ok=True)
    tbl = table_name(CATALOGUE, version)
    try:
        body = list(
            version_sql(
                sqlite_path, CATALOGUE, version, ("id", "portal", "host", "name", "url", "vote")
            )
        )
    except TooWide as e:
        print(f"d1: {CATALOGUE}@{version} not loaded: {e}")
        return []
    register = body.pop()
    body.insert(0, f'DROP TABLE IF EXISTS "{tbl}_fts";')
    fts = [
        f'CREATE VIRTUAL TABLE "{tbl}_fts" USING fts5(title, summary, publisher, '
        f"content='{tbl}', content_rowid='rowid', tokenize='porter unicode61 remove_diacritics 2');",
        *(
            f'INSERT INTO "{tbl}_fts" (rowid, title, summary, publisher) SELECT rowid, title, '
            f'summary, publisher FROM "{tbl}" WHERE rowid > {a} AND rowid <= {a + FTS_ROWS};'
            for a in range(0, n, FTS_ROWS)
        ),
    ]
    prune = []
    for v in sorted(set(loaded) - {version}):
        prune += [
            f'DROP TABLE IF EXISTS "{table_name(CATALOGUE, v)}_fts";',
            f'DROP TABLE IF EXISTS "{table_name(CATALOGUE, v)}";',
            f"DELETE FROM _versions WHERE slug = {literal(CATALOGUE)} AND version = {literal(v)};",
        ]
    # The new index is registered before the old one is dropped, so a search always has a table.
    stmts = [REGISTRY, *body, *fts, register, *prune]
    return _parts(out, f"{CATALOGUE}@{version}", stmts, stamp)


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
    """Adds the served datasets to the catalogue index file. Returns the version, which is a hash
    of the rows so a changed register loads again without waiting for a harvest."""
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
    tbl = table_name(SERVED, version)
    try:
        body = list(version_sql(tmp, SERVED, version, ("slug",)))
    except TooWide as e:
        print(f"d1: {SERVED}@{version} not loaded: {e}")
        return []
    finally:
        tmp.unlink()
    register = body.pop()
    text = "title, summary, publisher, keywords, fields"
    stmts = [
        REGISTRY,
        f'DROP TABLE IF EXISTS "{tbl}_fts";',
        *body,
        f"CREATE VIRTUAL TABLE \"{tbl}_fts\" USING fts5({text}, content='{tbl}', "
        "content_rowid='rowid', tokenize='porter unicode61 remove_diacritics 2');",
        f'INSERT INTO "{tbl}_fts" (rowid, {text}) SELECT rowid, {text} FROM "{tbl}";',
        register,
    ]
    for v in sorted(set(loaded) - {version}):
        stmts += [
            f'DROP TABLE IF EXISTS "{table_name(SERVED, v)}_fts";',
            f'DROP TABLE IF EXISTS "{table_name(SERVED, v)}";',
            f"DELETE FROM _versions WHERE slug = {literal(SERVED)} AND version = {literal(v)};",
        ]
    return _parts(out, f"{SERVED}@{version}", stmts, stamp)


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
        out = json.loads(r.stdout or "{}")
        if r.returncode != 0 or isinstance(out, dict):
            raise RuntimeError(f"D1 query failed: {sql[:80]}: {r.stdout[:300]} {r.stderr[:300]}")
        return [row for part in out for row in part.get("results", [])]


def registered(db) -> dict[tuple[str, str], int]:
    try:
        rows = db.query("SELECT slug, version, rows FROM _versions")
    except RuntimeError as e:
        if "no such table" in str(e):
            return {}
        raise
    return {(r["slug"], r["version"]): int(r["rows"]) for r in rows}


def holds(db, slug: str, version: str, expected: int) -> bool:
    """One COUNT per table: D1 caps compound SELECTs at five terms."""
    try:
        n = db.query(f'SELECT COUNT(*) AS n FROM "{table_name(slug, version)}"')[0]["n"]
    except RuntimeError:
        return False
    except LookupError:
        return False
    return n == expected


def restamp(path: Path, attempt: int) -> None:
    text = path.read_text(encoding="utf-8")
    first, rest = text.split("\n", 1) if text.startswith("-- load ") else ("-- load", text)
    path.write_text(f"{first} retry {attempt}\n{rest}", encoding="utf-8")


def _load_version(db, key: str, parts: list[Path], log, attempts: int) -> bool:
    slug, version = key.split("@", 1)
    for attempt in range(1, attempts + 1):
        errors = 0
        for p in parts:
            with IMPORT_LOCK:
                errors += 0 if db.file(p) else 1
        # D1 refuses a query while another version's import is in flight, so the check waits too.
        with IMPORT_LOCK:
            reg = registered(db)
            ok = (slug, version) in reg and holds(db, slug, version, reg[(slug, version)])
        log(
            f"d1 load: {key} attempt {attempt}: {len(parts)} parts, {errors} reported errors, {'verified' if ok else 'not verified'}"
        )
        if ok:
            return True
        for p in parts:
            restamp(p, attempt)
    return False


def _unregister(db, key: str, log) -> None:
    slug, version = key.split("@", 1)
    try:
        db.query(
            f"DELETE FROM _versions WHERE slug = {literal(slug)} AND version = {literal(version)}"
        )
        log(f"d1 load: {key} unregistered; the next deploy loads it again")
    except RuntimeError as e:
        # With no registry there is nothing to unregister; the version is not served either way.
        log(f"d1 load: {key} could not be unregistered: {e}")


def load(folder: Path, db, log=print, attempts: int = 2, workers: int = WORKERS) -> int:
    """Runs every part of each version, then checks the version is registered and its table holds
    its rows. An import can report an error after it has applied, so a version that fails the
    check loads again from its first part with a fresh stamp. A version still wrong is
    unregistered so the API never serves it. One version's failure never stops the others, and up
    to `workers` versions load at once. Returns the number of versions that did not load."""
    groups: dict[str, list[Path]] = {}
    for p in sorted(folder.glob("*.sql")):
        groups.setdefault(p.name.split(".part")[0], []).append(p)

    def one(key: str) -> bool:
        parts = groups[key]
        try:
            ok = _load_version(db, key, parts, log, attempts)
        except (RuntimeError, OSError, ValueError, KeyError, IndexError) as e:
            log(f"d1 load: {key} failed: {e}")
            ok = False
        if not ok:
            _unregister(db, key, log)
        return ok

    if workers <= 1:
        results = [one(k) for k in groups]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, groups))
    return sum(1 for ok in results if not ok)
