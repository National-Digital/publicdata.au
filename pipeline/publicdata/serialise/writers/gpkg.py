from __future__ import annotations

import sqlite3
import struct
from typing import TYPE_CHECKING

from publicdata.serialise import SQLITE_TYPES, _meta_tables, json_view
from publicdata.serialise.geo import _connect, _with_geometry, geo_kind

if TYPE_CHECKING:
    from pathlib import Path

    from publicdata.normalise import Table

# OGC WKT 1 for the CRSs the register may declare. Each resolves to its EPSG code under GDAL 3.
GPKG_SRS = {
    7844: (
        "GDA2020",
        'GEOGCS["GDA2020",DATUM["Geocentric_Datum_of_Australia_2020",SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]],AUTHORITY["EPSG","1168"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","7844"]]',
    ),
    4326: (
        "WGS 84",
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]',
    ),
    4283: (
        "GDA94",
        'GEOGCS["GDA94",DATUM["Geocentric_Datum_of_Australia_1994",SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]],AUTHORITY["EPSG","6283"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4283"]]',
    ),
}


def _gpkg_point(x: float, y: float, srs: int) -> bytes:
    # GeoPackageBinary header: magic, version, flags (little endian, XY envelope), srs, envelope, WKB point.
    return (
        b"GP\x00\x03"
        + struct.pack("<i", srs)
        + struct.pack("<dddd", x, x, y, y)
        + b"\x01"
        + struct.pack("<I", 1)
        + struct.pack("<dd", x, y)
    )


def gpkg_schema(con: sqlite3.Connection, srs: int, note: str) -> None:
    """The GeoPackage system tables, with the one coordinate system the layer uses."""
    con.execute("PRAGMA application_id = 1196444487")
    con.execute("PRAGMA user_version = 10300")
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.executescript(
        """
        CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
          organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
        CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
          description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
          CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
        CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
          srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
          CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
          CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
          CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
        """
    )
    con.executemany(
        "INSERT INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "Undefined cartesian SRS",
                -1,
                "NONE",
                -1,
                "undefined",
                "undefined cartesian coordinate reference system",
            ),
            (
                "Undefined geographic SRS",
                0,
                "NONE",
                0,
                "undefined",
                "undefined geographic coordinate reference system",
            ),
            (GPKG_SRS[srs][0], srs, "EPSG", srs, GPKG_SRS[srs][1], note),
        ],
    )


def write_gpkg(tbl: Table, header: dict, path: Path) -> None:
    """A GeoPackage with one point layer, records, plus the fields and publicdata tables."""
    if path.exists():
        path.unlink()
    if geo_kind(tbl.dataset) in ("polygon", "line"):
        return _write_shapes(tbl, header, path)
    ds = tbl.dataset
    g = ds.geometry
    srs = int(str(g.get("crs", "EPSG:7844")).split(":")[-1])
    if srs not in GPKG_SRS:
        msg = f"{ds.slug}: no GeoPackage definition for EPSG:{srs}"
        raise ValueError(msg)
    lon, lat = g["lon"], g["lat"]
    names = [f.name for f in ds.fields]
    cols = [f'"{f.name}" {SQLITE_TYPES[f.type]}' for f in ds.fields]
    if "suppressed" in tbl.table.column_names:
        names.append("suppressed")
        cols.append('"suppressed" TEXT')
    con = sqlite3.connect(path)
    gpkg_schema(con, srs, g.get("crs_note", ""))
    con.execute(
        f"CREATE TABLE records (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom POINT, {', '.join(cols)})"
    )
    _meta_tables(con, tbl, header)
    q = f"INSERT INTO records (geom, {', '.join(chr(34) + n + chr(34) for n in names)}) VALUES ({', '.join('?' * (len(names) + 1))})"
    t = json_view(tbl.table)
    bounds = [None, None, None, None]
    for b in t.to_batches(20_000):
        rows = []
        for row in b.to_pylist():
            if "suppressed" in row and row["suppressed"] is not None:
                row["suppressed"] = ";".join(row["suppressed"]) or None
            x, y = row.get(lon), row.get(lat)
            geom = None
            if x is not None and y is not None:
                geom = _gpkg_point(float(x), float(y), srs)
                bounds = [
                    x if bounds[0] is None else min(bounds[0], x),
                    y if bounds[1] is None else min(bounds[1], y),
                    x if bounds[2] is None else max(bounds[2], x),
                    y if bounds[3] is None else max(bounds[3], y),
                ]
            rows.append((geom, *(row[n] for n in names)))
        con.executemany(q, rows)
    con.execute(
        "INSERT INTO gpkg_contents VALUES (?, 'features', ?, ?, ?, ?, ?, ?, ?, ?)",
        ("records", ds.slug, ds.title, header["version"] + "T00:00:00.000Z", *bounds, srs),
    )
    con.execute(
        "INSERT INTO gpkg_geometry_columns VALUES ('records', 'geom', 'POINT', ?, 0, 0)", (srs,)
    )
    if ds.key:
        con.execute(
            f"CREATE INDEX records_key ON records ({', '.join(chr(34) + k + chr(34) for k in ds.key)})"
        )
    con.commit()
    con.execute("VACUUM")
    con.close()
    return None


def _gpkg_blob(wkb: bytes, env: tuple[float, float, float, float], srs: int) -> bytes:
    # GeoPackageBinary header: magic, version, flags (little endian, XY envelope), srs, envelope.
    minx, maxx, miny, maxy = env
    return (
        b"GP\x00\x03" + struct.pack("<i", srs) + struct.pack("<dddd", minx, maxx, miny, maxy) + wkb
    )


def _write_shapes(tbl, header: dict, path: Path) -> None:
    """A GeoPackage with one polygon or line layer, records, in GDA2020."""
    ds = tbl.dataset
    srs = 7844
    con = _connect()
    _with_geometry(con, tbl)
    shapes = con.execute(
        "SELECT ST_AsWKB(geometry), ST_XMin(geometry), ST_XMax(geometry), ST_YMin(geometry), "
        "ST_YMax(geometry) FROM t"
    ).fetchall()
    con.close()
    names = [f.name for f in ds.fields]
    cols = [f'"{f.name}" {SQLITE_TYPES[f.type]}' for f in ds.fields]
    db = sqlite3.connect(path)
    gpkg_schema(db, srs, ds.geometry.get("crs_note", ""))
    db.execute(
        f"CREATE TABLE records (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom GEOMETRY, {', '.join(cols)})"
    )
    _meta_tables(db, tbl, header)
    q = f"INSERT INTO records (geom, {', '.join(chr(34) + n + chr(34) for n in names)}) VALUES ({', '.join('?' * (len(names) + 1))})"
    t = json_view(tbl.table)
    bounds = [None, None, None, None]
    i = 0
    for b in t.to_batches(5_000):
        out = []
        for row in b.to_pylist():
            wkb, x0, x1, y0, y1 = shapes[i]
            i += 1
            blob = None
            if wkb is not None:
                blob = _gpkg_blob(bytes(wkb), (x0, x1, y0, y1), srs)
                bounds = [
                    x0 if bounds[0] is None else min(bounds[0], x0),
                    y0 if bounds[1] is None else min(bounds[1], y0),
                    x1 if bounds[2] is None else max(bounds[2], x1),
                    y1 if bounds[3] is None else max(bounds[3], y1),
                ]
            out.append((blob, *(row[n] for n in names)))
        db.executemany(q, out)
    db.execute(
        "INSERT INTO gpkg_contents VALUES (?, 'features', ?, ?, ?, ?, ?, ?, ?, ?)",
        ("records", ds.slug, ds.title, header["version"] + "T00:00:00.000Z", *bounds, srs),
    )
    kind = "MULTILINESTRING" if geo_kind(ds) == "line" else "MULTIPOLYGON"
    db.execute(
        "INSERT INTO gpkg_geometry_columns VALUES ('records', 'geom', ?, ?, 0, 0)", (kind, srs)
    )
    db.commit()
    db.execute("VACUUM")
    db.close()
