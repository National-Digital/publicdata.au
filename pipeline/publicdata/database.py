"""A database dataset built from the publisher's archive of delimited tables.

The archive becomes one DuckDB file, one Parquet file per table, a schema and a SQL script. Rows
are typed and columns renamed as for a single table; nothing is joined, except in the views the
publisher ships beside the tables.

The archive is read a table at a time through DuckDB, so a release of many gigabytes builds on
a runner with a few gigabytes of memory.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from .normalise import NormaliseError
from .rows import one_row
from .serialise import (
    DUCKDB_TYPES,
    SQL_TYPES,
    duckdb_comment,
    duckdb_connect,
    duckdb_meta,
    dumps,
    pretty,
    profile,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import duckdb

    from .jsontypes import JSON, JSONObject
    from .provenance import Header
    from .register import Dataset, Field, TableSpec
    from .store import Manifest

# What a build may hold in memory while loading one table. The runner has more, and the rest is
# left for the operating system's file cache, which the reads lean on.
MEMORY_LIMIT = "4GB"


@dataclass
class DatabaseOut:
    rows: int
    tables: dict[str, int]  # table -> rows
    unknown_columns: dict[str, list[str]] = field(default_factory=dict)  # table -> held columns
    unknown_tables: list[str] = field(default_factory=list)  # upstream tables with no entry


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def members(ds: Dataset, names: list[str]) -> dict[str, list[str]]:
    """The archive's members grouped by the upstream table the register's pattern names.

    Members keep archive order within each table.
    """
    rx = re.compile(ds.database_spec().member_match)
    out: dict[str, list[str]] = {}
    for n in names:
        m = rx.search(n)
        if m:
            out.setdefault(m.group("table"), []).append(n)
    return out


def _header(z: zipfile.ZipFile, member: str, encoding: str, delimiter: str) -> list[str]:
    with z.open(member) as f:
        line = f.readline().decode(encoding).lstrip("﻿").rstrip("\r\n")
    return [h.strip() for h in line.split(delimiter)]


def _expr(f: Field, dtype: str) -> str:
    """The typed value of a text column, by the field's declared type.

    A value that cannot take the type stops the build, as it does for a single table.
    """
    src = _ident(f.source)
    for v in f.null_values:
        src = f"NULLIF({src}, {_lit(v)})"
    if f.type == "string":
        return src
    if f.type == "boolean":
        t = ", ".join(_lit(v) for v in f.true_values)
        fl = ", ".join(_lit(v) for v in f.false_values)
        return (
            f"CASE WHEN {src} IS NULL THEN NULL WHEN {src} IN ({t}) THEN true "
            f"WHEN {src} IN ({fl}) THEN false ELSE error('not a boolean: ' || {src}) END"
        )
    if f.type == "date" and f.date_format != "%Y-%m-%d":
        return f"CAST(strptime({src}, {_lit(f.date_format)}) AS DATE)"
    return f"CAST({src} AS {dtype})"


def _load_table(  # noqa: PLR0913 - the options are keyword-only and named at each call
    con: duckdb.DuckDBPyConnection,
    z: zipfile.ZipFile,
    ds: Dataset,
    t: TableSpec,
    files: list[str],
    *,
    tmp: Path,
) -> int:
    """Create the typed table and load each of its members in turn.

    Members are extracted one at a time so the disk holds one member's text, not a whole table's.
    Members load in archive order.
    """
    db = ds.database_spec()
    cols = [f"{_ident(f.name)} {DUCKDB_TYPES[f.type]}" for f in t.fields]
    con.execute(f"CREATE TABLE {_ident(t.name)} ({', '.join(cols)})")
    exprs = ", ".join(f"{_expr(f, DUCKDB_TYPES[f.type])} AS {_ident(f.name)}" for f in t.fields)
    delim = "\\t" if db.delimiter == "\t" else db.delimiter.replace("'", "''")
    dest = tmp / f"{t.name}.txt"
    for name in files:
        with z.open(name) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        con.execute(
            f"INSERT INTO {_ident(t.name)} SELECT {exprs} FROM read_csv({_lit(dest.as_posix())}, "
            f"delim='{delim}', header=true, all_varchar=true, nullstr='', "
            f"encoding={_lit(db.encoding)}, quote='\"', escape='\"')"
        )
        dest.unlink()
    n: int = one_row(con.execute(f"SELECT count(*) FROM {_ident(t.name)}"))[0]
    return n


def _write_parquet(
    con: duckdb.DuckDBPyConnection, t: TableSpec, header: Header, path: Path, *, profiled: bool
) -> None:
    """One table in the publisher's order.

    The table is read under the Parquet profile, a row group at a time, for a version fetched
    since, and else as the version was first published.
    """
    size = profile.ROW_GROUP_ROWS if profiled else 65_536
    reader = con.execute(f"SELECT * FROM {_ident(t.name)}").to_arrow_reader(size)
    if not profiled:
        schema = reader.schema.with_metadata({"publicdata": dumps(header)})
        opts: profile.WriterOptions = {"compression": "zstd", "write_statistics": True}
    else:
        schema = reader.schema.with_metadata(profile.metadata(header))
        opts = profile.options(schema)
    with pq.ParquetWriter(path, schema, **opts) as w:
        for b in reader:
            w.write_batch(b, row_group_size=size if profiled else None)


def _frictionless_field(f: Field) -> JSONObject:
    d: JSONObject = {"name": f.name, "type": f.type, "title": f.source}
    if f.description:
        d["description"] = f.description
    if f.type == "date" and f.date_format != "%Y-%m-%d":
        d["format"] = f.date_format
    if f.type == "boolean":
        d["trueValues"] = list(f.true_values)
        d["falseValues"] = list(f.false_values)
    return d


def schema_json(ds: Dataset, rows: dict[str, int] | None = None) -> JSONObject:
    """The tables as Frictionless Table Schemas with their keys and references, and the views."""
    tables: list[JSON] = []
    for t in ds.tables:
        d: JSONObject = {
            "name": t.name,
            "title": t.source,
            "description": t.description,
            "fields": [_frictionless_field(f) for f in t.fields],
            "missingValues": [""],
        }
        if rows is not None:
            d["rows"] = rows.get(t.name, 0)
        if t.key:
            d["primaryKey"] = list(t.key)
        refs: list[JSON] = [
            {
                "fields": [f.name],
                "reference": {
                    "resource": f.references.split(".", 1)[0],
                    "fields": [f.references.split(".", 1)[1]],
                },
            }
            for f in t.fields
            if f.references
        ]
        if refs:
            d["foreignKeys"] = refs
        tables.append(d)
    return {
        "kind": "database",
        "tables": tables,
        "views": [{"name": v.name, "description": v.description, "sql": v.sql} for v in ds.views],
    }


def schema_sql(ds: Dataset, header: Header) -> str:
    """CREATE TABLE for every table with its keys and references, and the views.

    It also says how to load the Parquet files, for PostgreSQL and most SQL dialects.
    """
    files = header["url"].rsplit("/", 1)[0] + "/"
    lines = [
        f"-- {ds.title}, version {header['version']}",
        f"-- {header['attribution']}",
        f"-- Files: {files}",
        "--",
        "-- DuckDB needs none of this: ATTACH '" + files + "data.duckdb' AS db (READ_ONLY);",
        "-- Elsewhere, create the tables below and load each from tables/<table>.parquet.",
        "",
    ]
    for t in ds.tables:
        cols = [f"  {_ident(f.name)} {SQL_TYPES[f.type]}" for f in t.fields]
        if t.key:
            cols.append(f"  PRIMARY KEY ({', '.join(_ident(k) for k in t.key)})")
        for f in t.fields:
            if f.references:
                rt, rf = f.references.split(".", 1)
                cols.append(
                    f"  FOREIGN KEY ({_ident(f.name)}) REFERENCES {_ident(rt)} ({_ident(rf)})"
                )
        lines += [f"CREATE TABLE {_ident(t.name)} (", ",\n".join(cols), ");"]
        if t.description:
            lines.append(f"COMMENT ON TABLE {_ident(t.name)} IS {_lit(t.description)};")
        for f in t.fields:
            if f.description:
                lines.append(
                    f"COMMENT ON COLUMN {_ident(t.name)}.{_ident(f.name)} IS {_lit(f.description)};"
                )
        lines.append("")
    for v in ds.views:
        if v.description:
            lines.append(f"-- {v.description}")
        lines += [f"CREATE VIEW {_ident(v.name)} AS", v.sql.rstrip(";") + ";", ""]
    return "\n".join(lines)


def build_database(  # noqa: PLR0915 - a database's build steps, read in order
    ds: Dataset, m: Manifest, src: Path, vdir: Path, hdr: Callable[[int, str], Header]
) -> DatabaseOut:
    """Write data.duckdb, tables/<name>.parquet, schema.json and schema.sql into vdir.

    `hdr(rows, rel)` gives the provenance header for a file. Tables load in register order.
    """
    if m.ext != "zip":
        msg = f"{ds.slug}: a database source is a zip, not .{m.ext}"
        raise NormaliseError(msg)
    db = ds.database_spec()
    (vdir / "tables").mkdir(parents=True, exist_ok=True)
    out = DatabaseOut(0, {})
    with zipfile.ZipFile(src) as z, tempfile.TemporaryDirectory(prefix="publicdata-db-") as tmpdir:
        tmp = Path(tmpdir)
        by_source = members(ds, z.namelist())
        declared = {t.source for t in ds.tables}
        out.unknown_tables = sorted(s for s in by_source if s not in declared)
        missing = [t.source for t in ds.tables if t.source not in by_source]
        if missing:
            msg = f"{ds.slug}: the archive has no member for {missing}"
            raise NormaliseError(msg)
        con = duckdb_connect(vdir / "data.duckdb", None, threads=None)
        try:
            con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
            con.execute(f"SET temp_directory = {_lit((tmp / 'spill').as_posix())}")
            for t in ds.tables:
                files = by_source[t.source]
                allow = {f.source for f in t.fields}
                headers = {tuple(_header(z, f, db.encoding, db.delimiter)) for f in files}
                if len(headers) != 1:
                    msg = f"{ds.slug}: {t.name}: members have different headers"
                    raise NormaliseError(msg)
                header = list(next(iter(headers)))
                lacking = sorted(allow - set(header))
                if lacking:
                    msg = f"{ds.slug}: {t.name}: columns not in the file: {lacking}"
                    raise NormaliseError(msg)
                unknown = [h for h in header if h not in allow]
                if unknown:
                    out.unknown_columns[t.name] = unknown
                n = _load_table(con, z, ds, t, files, tmp=tmp)
                out.tables[t.name] = n
                out.rows += n
                duckdb_comment(con, t.name, None, t.description or t.source)
                for f in t.fields:
                    duckdb_comment(con, t.name, f.name, f.description or f.source)
                _write_parquet(
                    con,
                    t,
                    hdr(n, f"tables/{t.name}.parquet"),
                    vdir / "tables" / f"{t.name}.parquet",
                    profiled=bool(m.parquet),
                )
            for v in ds.views:
                con.execute(f"CREATE VIEW {_ident(v.name)} AS {v.sql.rstrip(';')}")
                duckdb_comment(con, v.name, None, v.description)
            duckdb_meta(con, hdr(out.rows, "data.duckdb"), [])
            con.execute("DROP TABLE fields")
            con.execute(
                'CREATE TABLE fields ("table" VARCHAR, name VARCHAR, type VARCHAR, source VARCHAR, description VARCHAR)'
            )
            con.executemany(
                "INSERT INTO fields VALUES (?, ?, ?, ?, ?)",
                [
                    (t.name, f.name, f.type, f.source, f.description)
                    for t in ds.tables
                    for f in t.fields
                ],
            )
            con.execute(
                "CREATE TABLE tables (name VARCHAR PRIMARY KEY, source VARCHAR, rows BIGINT, description VARCHAR)"
            )
            con.executemany(
                "INSERT INTO tables VALUES (?, ?, ?, ?)",
                [(t.name, t.source, out.tables[t.name], t.description) for t in ds.tables],
            )
            con.execute(
                'CREATE TABLE relations ("table" VARCHAR, field VARCHAR, references_table VARCHAR, references_field VARCHAR)'
            )
            relations = [
                (t.name, f.name, *f.references.split(".", 1))
                for t in ds.tables
                for f in t.fields
                if f.references
            ]
            if relations:
                con.executemany("INSERT INTO relations VALUES (?, ?, ?, ?)", relations)
            con.execute("CHECKPOINT")
        finally:
            con.close()
    (vdir / "schema.json").write_text(pretty(schema_json(ds, out.tables)), encoding="utf-8")
    (vdir / "schema.sql").write_text(schema_sql(ds, hdr(out.rows, "schema.sql")), encoding="utf-8")
    return out
