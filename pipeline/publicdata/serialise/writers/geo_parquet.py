from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .. import dumps, profile
from ..geo import _connect, _with_geometry

_PROJJSON: dict[str, dict] = {}


def projjson(crs: str) -> dict:
    """The coordinate system's PROJJSON, as DuckDB's own GeoParquet writer states it."""
    if crs not in _PROJJSON:
        import tempfile

        con = _connect()
        with tempfile.TemporaryDirectory() as t:
            con.execute(
                f"COPY (SELECT ST_SetCRS(ST_Point(0, 0), '{crs}') AS g) TO '{t}/c.parquet' (FORMAT parquet)"
            )
            geo = json.loads(pq.read_metadata(f"{t}/c.parquet").metadata[b"geo"])
        con.close()
        _PROJJSON[crs] = geo["columns"]["g"]["crs"]
    return _PROJJSON[crs]


def write_shape_parquet(tbl, header: dict, path: Path, lay: dict | None = None) -> None:
    """A layer's Parquet: the fields as data.parquet always holds them, then the WKB geometry in
    GDA2020, with the GeoParquet metadata, so every other format of the layer can be made from it."""
    from ...spine import DATUM

    t = tbl.table.append_column("geometry", tbl.geometry.cast(pa.binary()))
    kinds = (
        ["Polygon", "MultiPolygon"]
        if tbl.dataset.geometry["kind"] == "polygon"
        else ["LineString", "MultiLineString"]
    )
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {"encoding": "WKB", "geometry_types": kinds, "crs": projjson(DATUM)}
        },
    }
    lay = tbl.manifest.parquet if lay is None else lay
    if not lay:
        t = t.replace_schema_metadata({"publicdata": dumps(header), "geo": dumps(geo)})
        pq.write_table(t, path, compression="zstd", write_statistics=True, row_group_size=65_536)
        return
    perm = profile.order_of(tbl, lay["sort"], lay["key"])
    profile.write(t, header, path, lay, perm, extra={"geo": dumps(geo)})


def write_geo_parquet(tbl, header: dict, path: Path) -> None:
    """The rows with a WKB geometry column and the GeoParquet metadata, the datum in PROJJSON."""
    con = _connect()
    _with_geometry(con, tbl)
    meta = dumps(header).replace("'", "''")
    con.execute(
        f"COPY t TO '{path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 65536, "
        f"KV_METADATA {{publicdata: '{meta}'}})"
    )
    con.close()
