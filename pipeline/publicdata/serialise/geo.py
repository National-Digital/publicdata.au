"""What the geometry writers share.

That is a dataset's geometry kind, and its rows with their geometry in a DuckDB table. The
writers themselves are modules under writers/.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from publicdata.spine import DATUM, connect

from . import json_view

if TYPE_CHECKING:
    import duckdb

    from publicdata.normalise import Table
    from publicdata.register import Dataset

WEB_MERCATOR = 20037508.342789244
TILE_EXTENT = 4096
MAXZOOM = 10
MVT_TYPES = {"integer": "Number", "number": "Number", "boolean": "Boolean"}


def geo_kind(ds: Dataset) -> str:
    """'' for a table with no geometry, else point, polygon or line."""
    return ds.geometry.get("kind", "") if ds.geometry else ""


def _connect() -> duckdb.DuckDBPyConnection:
    con = connect()
    # One thread: an aggregate's input order, and so each file's bytes, are then the same each run.
    con.execute("SET threads = 1")
    con.execute("SET preserve_insertion_order = true")
    return con


def _crs(ds: Dataset) -> str:
    return DATUM if geo_kind(ds) != "point" else str(ds.geometry_spec().get("crs") or DATUM)


def _with_geometry(con: duckdb.DuckDBPyConnection, tbl: Table) -> None:
    """A table `t` in the connection holding the rows with their geometry as `geometry`."""
    ds = tbl.dataset
    t = json_view(tbl.table)
    if geo_kind(ds) == "point":
        con.register("rows_in", t)
        g = ds.geometry_spec()
        geom = (
            f'CASE WHEN "{g["lon"]}" IS NULL OR "{g["lat"]}" IS NULL THEN NULL '
            f'ELSE ST_Point("{g["lon"]}", "{g["lat"]}") END'
        )
    else:
        con.register("rows_in", t.append_column("__wkb", tbl.shapes()))
        geom = "ST_GeomFromWKB(__wkb)"
    cols = ", ".join(f'"{c}"' for c in t.column_names)
    con.execute(
        f"CREATE OR REPLACE TABLE t AS SELECT {cols}, ST_SetCRS({geom}, '{_crs(ds)}') AS geometry "
        "FROM rows_in"
    )
    con.unregister("rows_in")
