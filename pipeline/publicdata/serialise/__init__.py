"""Pure serialisers: one normalised table in, files out. No network, no clock.

Every writer takes the same Table and the provenance header, and writes deterministic
bytes. Two runs over the same snapshot are byte-identical, which CI checks.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import xlsxwriter

from publicdata.normalise import Table

# Every whole-table format, in the order the site lists them. A version whose store manifest has
# no `caps` stamp keeps the set it was built with, Arrow included.
FORMATS = ("json", "ndjson", "csv", "parquet", "sqlite", "duckdb", "xlsx", "csv.gz")
LEGACY_FORMATS = ("json", "ndjson", "csv", "parquet", "sqlite", "duckdb", "xlsx", "arrow", "csv.gz")
GEO_FORMATS = ("geojson", "gpkg", "geo.parquet")
# A polygon or line layer keeps its shapes in data.parquet itself, which is GeoParquet, and adds
# vector tiles for maps.
SHAPE_FORMATS = ("geojson", "gpkg", "pmtiles")
EXCEL_MAX_ROWS = 1_048_575
# Before the caps, a JSON document that holds the whole table stopped here.
JSON_MAX_ROWS = 2_000_000
# The files every capped version writes first, whose sizes decide the formats measured on them.
MEASURED = ("ndjson", "csv")
# Each capped format: the file it is measured on, and the most bytes that file may hold. JSON
# follows the NDJSON, and SQLite and Excel the CSV. GeoJSON is measured on itself, since a layer's
# shapes are in no other text file.
CAPS = {
    "sqlite": ("csv", 500_000_000),
    "geojson": ("geojson", 100_000_000),
    "xlsx": ("csv", 50_000_000),
    "json": ("ndjson", 50_000_000),
}
MEDIA = {
    "json": "application/json",
    "ndjson": "application/x-ndjson",
    "csv": "text/csv",
    "parquet": "application/vnd.apache.parquet",
    "sqlite": "application/vnd.sqlite3",
    # DuckDB registers no media type; its files are served as bytes.
    "duckdb": "application/octet-stream",
    "geojson": "application/geo+json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "arrow": "application/vnd.apache.arrow.file",
    "csv.gz": "application/gzip",
    "gpkg": "application/geopackage+sqlite3",
    "geo.parquet": "application/vnd.apache.parquet",
    "pmtiles": "application/vnd.pmtiles",
    "sql": "application/sql",
    "csv-metadata.json": "application/csvm+json",
}
FORMAT_LABEL = {
    "json": "JSON",
    "ndjson": "NDJSON",
    "csv": "CSV",
    "parquet": "Parquet",
    "sqlite": "SQLite",
    "duckdb": "DuckDB",
    "geojson": "GeoJSON",
    "xlsx": "Excel",
    "arrow": "Arrow",
    "csv.gz": "CSV (gzip)",
    "gpkg": "GeoPackage",
    "geo.parquet": "GeoParquet",
    "pmtiles": "PMTiles",
}


# The formats a build is limited to, set by `build --formats`; None is every format. CI builds
# with a subset first to prove a cached version grows into the full set without a rebuild.
LIMIT: set[str] | None = None


def capped(manifest) -> bool:
    """Whether a version takes the capped format set: its store manifest, or the built manifest's
    dict, carries the fetch's caps stamp.
    """
    caps = manifest.get("caps") if isinstance(manifest, dict) else getattr(manifest, "caps", 0)
    return bool(caps)


def _kind(geometry: bool | str) -> str:
    return "point" if geometry is True else (geometry or "")


def _geo_formats(kind: str) -> tuple[str, ...]:
    return () if not kind else GEO_FORMATS if kind == "point" else SHAPE_FORMATS


def cappable(geometry: bool | str) -> list[str]:
    """The formats the caps can leave out of a version of this kind, in CAPS order."""
    have = set(FORMATS) | set(_geo_formats(_kind(geometry)))
    return [f for f in CAPS if f in have]


def over_cap(fmt: str, rows: int, size: int) -> str | None:
    """Why a new version leaves fmt out, given the size of the file it is measured on, or None."""
    on, limit = CAPS[fmt]
    if fmt == "xlsx" and rows > EXCEL_MAX_ROWS:
        return _row_reason(fmt)
    if size <= limit:
        return None
    label, size_text = FORMAT_LABEL[fmt], _over(size, limit)
    if on == fmt:
        return (
            f"{label} is not offered because the file would be {size_text}, over the "
            f"{limit / 1e6:,.0f} MB limit for {label}."
        )
    return (
        f"{label} is not offered because the table is {size_text} as "
        f"{FORMAT_LABEL[on]}, over the {limit / 1e6:,.0f} MB limit for {label}."
    )


def _over(size: int, limit: int) -> str:
    """A size past a limit, in MB, or in bytes when MB to one place would not show it is over."""
    mb = f"{size / 1e6:,.1f}"
    return f"{mb} MB" if float(mb.replace(",", "")) > limit / 1e6 else f"{size:,} bytes"


def legacy_left_out(rows: int, geometry: bool | str) -> dict[str, str]:
    """The formats a version without the caps stamp lacks, by the row limits it was built under."""
    over = {"xlsx"} if rows > EXCEL_MAX_ROWS else set()
    if rows > JSON_MAX_ROWS:
        over |= {"json", "geojson"} if _kind(geometry) else {"json"}
    return {f: _row_reason(f) for f in sorted(over)}


def _row_reason(fmt: str) -> str:
    if fmt == "xlsx":
        return (
            f"Excel is not offered because the table is over {EXCEL_MAX_ROWS:,} rows, "
            "which is as many as a worksheet holds below its header row."
        )
    return (
        f"{FORMAT_LABEL[fmt]} is not offered because the table is over {JSON_MAX_ROWS:,} rows, "
        "too many for one document a reader parses at once."
    )


def reasons(rows: int, geometry: bool | str, gone: dict[str, str] | None) -> dict[str, str]:
    """Each format a version lacks, with the reason: a capped version's recorded
    formats_left_out, or the row limits of a version without the caps stamp.
    """
    return dict(gone) if gone is not None else legacy_left_out(rows, geometry)


def formats_for(rows: int, geometry: bool | str, gone: dict[str, str] | None = None) -> list[str]:
    """The formats a version carries. `geometry` is the dataset's geometry kind, or True for
    points. `gone` is a capped version's formats_left_out, as its manifest records it; None gives
    the set of a version without the caps stamp.
    """
    kind = _kind(geometry)
    base = LEGACY_FORMATS if gone is None else FORMATS
    over = reasons(rows, kind, gone)
    out = [f for f in (*base, *_geo_formats(kind)) if f not in over]
    return out if LIMIT is None else [f for f in out if f in LIMIT]


COMPACT = {"ensure_ascii": False, "separators": (",", ":")}


def dumps(o) -> str:
    return json.dumps(o, **COMPACT)


def pretty(o) -> str:
    return json.dumps(o, ensure_ascii=False, indent=2) + "\n"


def slugify(v) -> str:
    if v is None:
        return "_null"
    s = re.sub(r"[^a-z0-9]+", "-", str(v).lower()).strip("-")
    return s or "_blank"


def json_view(t: pa.Table) -> pa.Table:
    """Dates and timestamps as ISO strings so rows serialise without a custom encoder."""
    cols = []
    for name in t.column_names:
        c = t.column(name)
        if pa.types.is_date(c.type):
            c = pc.strftime(c, format="%Y-%m-%d")
        elif pa.types.is_timestamp(c.type):
            c = pc.strftime(c, format="%Y-%m-%dT%H:%M:%S")
        cols.append(c)
    return pa.table(cols, names=t.column_names)


def iter_rows(t: pa.Table, batch: int = 20_000):
    for b in t.to_batches(batch):
        yield from b.to_pylist()


def table_schema(tbl: Table) -> dict:
    from publicdata.spine import LAYERS, is_spine

    ds = tbl.dataset
    used = {p["layer"]: p for p in tbl.places}
    fields = []
    for f in ds.fields:
        d = {"name": f.name, "type": f.type, "title": f.source}
        if is_spine(f.source):
            key = f.source.removeprefix("(spine: ").rstrip(")")
            d["title"] = f.display
            d["publicdata:derived"] = {
                "method": "joined by location",
                "dataset": LAYERS[key].slug,
                "version": used.get(key, {}).get("version"),
            }
        if f.description:
            d["description"] = f.description
        if f.type == "date" and f.date_format != "%Y-%m-%d":
            d["format"] = f.date_format
        if f.type == "boolean":
            d["trueValues"] = list(f.true_values)
            d["falseValues"] = list(f.false_values)
        if f.note:
            d["publicdata:note"] = f.note
        fields.append(d)
    if "suppressed" in tbl.table.column_names:
        fields.append(
            {
                "name": "suppressed",
                "type": "array",
                "description": "Names of the fields the publisher suppressed in this row. "
                "The suppressed cells are null.",
            }
        )
    schema = {"fields": fields, "missingValues": [""]}
    if ds.key:
        schema["primaryKey"] = list(ds.key)
    if ds.suppression:
        schema["publicdata:suppressionTokens"] = list(ds.suppression)
    if ds.geometry:
        from publicdata.spine import DATUM

        kind = ds.geometry["kind"]
        schema["publicdata:geometry"] = {
            "kind": kind,
            "crs": DATUM if kind != "point" else ds.geometry.get("crs"),
            **(
                {"lon": ds.geometry["lon"], "lat": ds.geometry["lat"]}
                if kind == "point"
                else {"column": "geometry", "encoding": "WKB", "in": ["data.parquet"]}
            ),
        }
    return schema


SUPPRESSED_NOTE = (
    "Names of the fields the publisher suppressed in this row. The suppressed cells are null."
)


def field_rows(tbl: Table) -> list[tuple[str, str, str, str]]:
    """(name, type, publisher header, description) for every published column, suppressed included."""
    rows = [(f.name, f.type, f.source, f.description) for f in tbl.dataset.fields]
    if "suppressed" in tbl.table.column_names:
        rows.append(("suppressed", "array", "", SUPPRESSED_NOTE))
    return rows


SQLITE_TYPES = {
    "string": "TEXT",
    "integer": "INTEGER",
    "number": "REAL",
    "boolean": "INTEGER",
    "date": "TEXT",
    "datetime": "TEXT",
}


def _meta_tables(con: sqlite3.Connection, tbl: Table, header: dict) -> None:
    con.execute("CREATE TABLE publicdata (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE fields (name TEXT PRIMARY KEY, type TEXT, source TEXT, description TEXT)"
    )
    con.executemany(
        "INSERT INTO publicdata VALUES (?, ?)",
        [(k, v if isinstance(v, str) else dumps(v)) for k, v in header.items()],
    )
    con.executemany("INSERT INTO fields VALUES (?, ?, ?, ?)", field_rows(tbl))


DUCKDB_TYPES = {
    "string": "VARCHAR",
    "integer": "BIGINT",
    "number": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
}
# A table this small is written with 16 KB blocks, so the file is tens of kilobytes instead of
# the megabyte the default 256 KB blocks need before any rows go in.
DUCKDB_SMALL_ROWS = 1_000_000


def duckdb_connect(path: Path, rows: int | None, name: str = "db", threads: int | None = 1):
    """A DuckDB connection with a fresh database at path attached as `name` and made current.
    `rows` picks the block size: a small table takes 16 KB blocks, and None keeps DuckDB's
    default, as a database of many tables does. One thread by default: a table is written
    once, and a bounded build matters more than speed; a database build passes None and takes
    every core.
    """
    import duckdb

    if path.exists():
        path.unlink()
    con = duckdb.connect()
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    con.execute("SET preserve_insertion_order = true")
    opts = " (BLOCK_SIZE 16384)" if rows is not None and rows < DUCKDB_SMALL_ROWS else ""
    con.execute(f"ATTACH '{path.as_posix()}' AS {name}{opts}")
    con.execute(f"USE {name}")
    return con


def duckdb_meta(con, header: dict, fields: list[tuple[str, str, str, str]]) -> None:
    """The provenance and the field list as tables, as the SQLite file carries them."""
    con.execute("CREATE TABLE publicdata (key VARCHAR PRIMARY KEY, value VARCHAR)")
    con.execute(
        "CREATE TABLE fields (name VARCHAR, type VARCHAR, source VARCHAR, description VARCHAR)"
    )
    con.executemany(
        "INSERT INTO publicdata VALUES (?, ?)",
        [(k, v if isinstance(v, str) else dumps(v)) for k, v in header.items()],
    )
    if fields:
        con.executemany("INSERT INTO fields VALUES (?, ?, ?, ?)", fields)


def duckdb_comment(con, table: str, column: str | None, text: str) -> None:
    if not text:
        return
    t = text.replace("'", "''")
    if column is None:
        con.execute(f"COMMENT ON TABLE \"{table}\" IS '{t}'")
    else:
        con.execute(f'COMMENT ON COLUMN "{table}"."{column}" IS \'{t}\'')


def duckdb_digest(path: Path) -> str:
    """A digest of what a DuckDB file holds: every table's columns and rows in their stored
    order, every view's text, constraint, index and comment, the block size and the storage
    version. The bytes of two files written from the same rows differ, since the storage
    compresses by sampling, so the determinism check compares this instead.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("SET threads = 1")
    con.execute(f"ATTACH '{path.as_posix()}' AS d (READ_ONLY)")
    try:
        parts = []
        cols = con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_catalog = 'd' ORDER BY table_name, ordinal_position"
        ).fetchall()
        parts.append(dumps(cols))
        tables = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = 'd' "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"
        ).fetchall()
        for (t,) in tables:
            names = ", ".join(f'"{c}"' for tn, c, _ in cols if tn == t)
            # The row id is each row's place, so rows written in another order differ.
            n, h = con.execute(
                f'SELECT count(*), coalesce(bit_xor(hash(rowid, {names})), 0) FROM d."{t}"'
            ).fetchone()
            s = con.execute(
                f'SELECT coalesce(sum(hash(rowid, {names}) % 1000003), 0) FROM d."{t}"'
            ).fetchone()[0]
            parts.append(f"{t}:{n}:{h}:{s}")
        for q in (
            "SELECT block_size FROM pragma_database_size() WHERE database_name = 'd'",
            "SELECT tags FROM duckdb_databases() WHERE database_name = 'd'",
            "SELECT table_name, constraint_type, constraint_text FROM duckdb_constraints() "
            "WHERE database_name = 'd' ORDER BY ALL",
            "SELECT index_name, sql FROM duckdb_indexes() WHERE database_name = 'd' ORDER BY ALL",
        ):
            parts.append(dumps(con.execute(q).fetchall()))
        parts.append(
            dumps(
                con.execute(
                    "SELECT view_name, sql FROM duckdb_views() WHERE database_name = 'd' "
                    "AND NOT internal ORDER BY view_name"
                ).fetchall()
            )
        )
        parts.append(
            dumps(
                con.execute(
                    "SELECT table_name, comment FROM duckdb_tables() WHERE database_name = 'd' "
                    "ORDER BY table_name"
                ).fetchall()
            )
        )
        parts.append(
            dumps(
                con.execute(
                    "SELECT table_name, column_name, comment FROM duckdb_columns() "
                    "WHERE database_name = 'd' ORDER BY table_name, column_index"
                ).fetchall()
            )
        )
    finally:
        con.close()
    import hashlib

    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def write_partitions(tbl: Table, header_for, out: Path) -> dict:
    """by/<field>/<value>.json for every declared partition field. Returns an index."""
    index = {}
    for fname in tbl.dataset.partition_by:
        col = tbl.table.column(fname)
        values = sorted(set(col.to_pylist()), key=lambda v: (v is None, str(v)))
        d = out / "by" / fname
        d.mkdir(parents=True, exist_ok=True)
        entries = []
        seen = {}
        for v in values:
            s = slugify(v)
            if s in seen and seen[s] != v:
                raise ValueError(f"{fname}: '{v}' and '{seen[s]}' both slugify to {s}")
            seen[s] = v
            mask = pc.is_null(col) if v is None else pc.equal(col, v)
            part = tbl.table.filter(mask)
            h = header_for(part.num_rows, f"by/{fname}/{s}.json")
            # A date partition is named by its ISO date in JSON.
            jv = v.isoformat() if hasattr(v, "isoformat") else v
            h["partition"] = {"field": fname, "value": jv}
            write_json(tbl, h, d / f"{s}.json", rows=part)
            entry = {"value": jv, "rows": part.num_rows, "json": f"by/{fname}/{s}.json"}
            # Points only: a layer's shapes are one file each, in the layer's own formats.
            if (tbl.dataset.geometry or {}).get(
                "kind", "point"
            ) == "point" and tbl.dataset.geometry:
                hg = header_for(part.num_rows, f"by/{fname}/{s}.geojson")
                hg["partition"] = {"field": fname, "value": jv}
                write_geojson(tbl, hg, d / f"{s}.geojson", rows=part)
                entry["geojson"] = f"by/{fname}/{s}.geojson"
            entries.append(entry)
        (d / "index.json").write_text(
            pretty({"field": fname, "partitions": entries}), encoding="utf-8"
        )
        index[fname] = entries
    return index


def write_dictionary(ds, header: dict, path: Path) -> None:
    """The field list as a workbook: one row per field, then the provenance. The created date
    is the version date, so two builds write the same bytes.
    """
    when = dt.datetime.fromisoformat(header["version"] + "T00:00:00")
    wb = xlsxwriter.Workbook(str(path), {"default_date_format": "yyyy-mm-dd"})
    wb.set_properties(
        {
            "title": f"{ds.title}: data dictionary",
            "author": ds.publisher.name,
            "company": header["operator"]["name"],
            "comments": header["attribution"],
            "hyperlink_base": header["url"],
            "created": when,
        }
    )
    bold = wb.add_format({"bold": True})
    wrap = wb.add_format({"text_wrap": True, "valign": "top"})
    ws = wb.add_worksheet("fields")
    ws.write_row(0, 0, ["field", "type", "publisher_header", "description", "label"], bold)
    ws.freeze_panes(1, 0)
    for r, f in enumerate(ds.fields, start=1):
        ws.write_row(r, 0, [f.name, f.type, f.source, f.description, f.display], wrap)
    ws.set_column(0, 0, 32)
    ws.set_column(1, 1, 10)
    ws.set_column(2, 2, 32)
    ws.set_column(3, 3, 60)
    ws.set_column(4, 4, 28)
    ps = wb.add_worksheet("publicdata")
    rows = [
        ("dataset", ds.title),
        ("publisher", ds.publisher.name),
        ("licence", header["licence"]["title"]),
        ("attribution", header["attribution"]),
        ("version", header["version"]),
        ("version_url", header["url"]),
        ("source", header["source"]["url"] or ""),
        ("source_sha256", header["source"]["sha256"]),
        ("key", ", ".join(ds.key)),
        ("not_endorsed", header["not_endorsed"]),
    ]
    for r, (k, v) in enumerate(rows):
        ps.write(r, 0, k, bold)
        ps.write(r, 1, v)
    ps.set_column(0, 0, 16)
    ps.set_column(1, 1, 100)
    wb.close()


def _fixed_zip(path: Path, stamp: tuple) -> None:
    """Rewrite a zip so every entry carries one timestamp. XlsxWriter uses the clock. A zip
    cannot record a date before 1980, so an older version is stamped 1 January 1980.
    """
    stamp = max(tuple(stamp), (1980, 1, 1, 0, 0, 0))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=stamp)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            with src.open(info) as fin, dst.open(zi, "w") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
    tmp.replace(path)


SQL_TYPES = {
    "string": "text",
    "integer": "bigint",
    "number": "double precision",
    "boolean": "boolean",
    "date": "date",
    "datetime": "timestamp",
}


def schema_sql(tbl: Table, header: dict) -> str:
    """CREATE TABLE for PostgreSQL, with a COPY line for the CSV. Works in most SQL dialects."""
    ds = tbl.dataset
    name = ds.slug.replace("-", "_")
    lines = [
        f"-- {ds.title}, version {header['version']}",
        f"-- {header['attribution']}",
        f"-- Files: {header['url'].rsplit('/', 1)[0]}/",
        "",
        f"CREATE TABLE {name} (",
    ]
    cols = [f'  "{f.name}" {SQL_TYPES[f.type]}' for f in ds.fields]
    if "suppressed" in tbl.table.column_names:
        cols.append('  "suppressed" text')
    if ds.key:
        cols.append(f"  PRIMARY KEY ({', '.join(chr(34) + k + chr(34) for k in ds.key)})")
    lines += [",\n".join(cols), ");", ""]
    for fname, _type, _src, desc in field_rows(tbl):
        if desc:
            d = desc.replace("'", "''")
            lines.append(f"COMMENT ON COLUMN {name}.\"{fname}\" IS '{d}';")
    lines += [
        "",
        f"-- PostgreSQL: \\copy {name} FROM 'data.csv' WITH (FORMAT csv, HEADER true)",
        "",
    ]
    return "\n".join(lines)


CSVW_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "double",
    "boolean": "boolean",
    "date": "date",
    "datetime": "dateTime",
}


def csvw_metadata(tbl: Table, header: dict) -> dict:
    """W3C CSV on the Web metadata for data.csv."""
    ds = tbl.dataset
    columns = []
    for f in ds.fields:
        c = {"name": f.name, "titles": [f.name, f.source], "datatype": CSVW_TYPES[f.type]}
        if f.description:
            c["dc:description"] = f.description
        columns.append(c)
    if "suppressed" in tbl.table.column_names:
        columns.append(
            {
                "name": "suppressed",
                "titles": ["suppressed"],
                "datatype": "string",
                "separator": ";",
                "dc:description": "Fields the publisher suppressed in this row. The cells are null.",
            }
        )
    schema = {"columns": columns}
    if ds.key:
        schema["primaryKey"] = list(ds.key)
    return {
        "@context": "http://www.w3.org/ns/csvw",
        "url": "data.csv",
        "dc:title": ds.title,
        "dc:publisher": {"schema:name": ds.publisher.name, "schema:url": ds.publisher.url},
        "dc:license": ds.licence.url,
        "dc:source": header["source"]["url"],
        "dc:identifier": header["url"].rsplit("/", 1)[0] + "/",
        "dc:bibliographicCitation": header["cite"],
        "dialect": {
            "header": True,
            "encoding": "utf-8",
            "delimiter": ",",
            "lineTerminators": ["\n"],
        },
        "null": [""],
        "tableSchema": schema,
    }


# The writers, one module each under writers/, called as write(tbl, header, path, vdir). The
# build cache keys each on its own module, so adding or changing one rewrites only its file in
# every cached version. The JSON and GeoJSON writers also make the partition files above, so
# they count as code that shapes a version (cache.ROW_WRITERS).
from .writers.arrow import write_arrow  # noqa: E402
from .writers.csv import write_csv  # noqa: E402
from .writers.csv_gz import write_csv_gz  # noqa: E402
from .writers.duckdb import write_duckdb  # noqa: E402
from .writers.geo_parquet import write_geo_parquet, write_shape_parquet  # noqa: E402
from .writers.geojson import write_geojson  # noqa: E402
from .writers.gpkg import write_gpkg  # noqa: E402
from .writers.json import write_json  # noqa: E402
from .writers.ndjson import write_ndjson  # noqa: E402
from .writers.parquet import write_parquet  # noqa: E402
from .writers.pmtiles import write_pmtiles  # noqa: E402
from .writers.sqlite import write_sqlite  # noqa: E402
from .writers.xlsx import write_xlsx  # noqa: E402

WRITERS = {
    "json": lambda tbl, header, path, vdir: write_json(tbl, header, path),
    "ndjson": lambda tbl, header, path, vdir: write_ndjson(tbl, header, path),
    "csv": lambda tbl, header, path, vdir: write_csv(tbl, path),
    "parquet": lambda tbl, header, path, vdir: (
        write_shape_parquet(tbl, header, path)
        if tbl.geometry is not None
        else write_parquet(tbl, header, path)
    ),
    "sqlite": lambda tbl, header, path, vdir: write_sqlite(tbl, header, path),
    "duckdb": lambda tbl, header, path, vdir: write_duckdb(tbl, header, path),
    "xlsx": lambda tbl, header, path, vdir: write_xlsx(tbl, header, path),
    "arrow": lambda tbl, header, path, vdir: write_arrow(tbl, header, path),
    "csv.gz": lambda tbl, header, path, vdir: write_csv_gz(tbl, path, vdir),
    "geojson": lambda tbl, header, path, vdir: write_geojson(tbl, header, path),
    "gpkg": lambda tbl, header, path, vdir: write_gpkg(tbl, header, path),
    "geo.parquet": lambda tbl, header, path, vdir: write_geo_parquet(tbl, header, path),
    "pmtiles": lambda tbl, header, path, vdir: write_pmtiles(tbl, header, path),
}
assert set(WRITERS) == {*LEGACY_FORMATS, *GEO_FORMATS, *SHAPE_FORMATS}
# The module that writes each format, for the cache's writer keys, and the formats a writer
# derives its file from, whose modules its key takes in too.
WRITER_MODULES = {fmt: fmt.replace(".", "_") for fmt in WRITERS}
WRITER_DEPENDS = {"csv.gz": ("csv",)}
# The writer a format runs for a table and for a shape layer, so each is keyed on its own.
WRITER_VARIANTS = {"parquet": {False: write_parquet, True: write_shape_parquet}}
