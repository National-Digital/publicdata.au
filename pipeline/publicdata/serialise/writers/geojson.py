from __future__ import annotations

from typing import TYPE_CHECKING

from publicdata.serialise import dumps, iter_rows, json_view
from publicdata.serialise.geo import _connect, _with_geometry, geo_kind

if TYPE_CHECKING:
    from pathlib import Path

    import pyarrow as pa

    from publicdata.normalise import Table


def write_geojson(tbl: Table, header: dict, path: Path, rows: pa.Table | None = None) -> None:
    if geo_kind(tbl.dataset) in ("polygon", "line"):
        return _write_shapes(tbl, header, path)
    g = tbl.dataset.geometry
    t = json_view(rows if rows is not None else tbl.table)
    lon, lat = g["lon"], g["lat"]
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write('{"type":"FeatureCollection","publicdata":')
        f.write(dumps(header))
        f.write(',"crs_note":' + dumps(g.get("crs_note", "")))
        f.write(',"features":[')
        first = True
        for row in iter_rows(t):
            if not first:
                f.write(",")
            first = False
            x, y = row.get(lon), row.get(lat)
            geom = (
                {"type": "Point", "coordinates": [x, y]}
                if x is not None and y is not None
                else None
            )
            f.write("\n")
            f.write(dumps({"type": "Feature", "geometry": geom, "properties": row}))
        f.write("\n]}\n")


def _write_shapes(tbl, header: dict, path: Path) -> None:
    """A FeatureCollection with one feature per row, its polygon or line, in GDA2020."""
    con = _connect()
    _with_geometry(con, tbl)
    names = [
        c
        for c in con.execute("SELECT * FROM t LIMIT 0").fetch_arrow_table().column_names
        if c != "geometry"
    ]
    cur = con.execute("SELECT ST_AsGeoJSON(geometry) FROM t")
    rows = json_view(tbl.table)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write('{"type":"FeatureCollection","publicdata":')
        f.write(dumps(header))
        f.write(',"crs_note":' + dumps(tbl.dataset.geometry.get("crs_note", "")))
        f.write(',"features":[')
        first = True
        for b in rows.to_batches(5_000):
            geoms = cur.fetchmany(b.num_rows)
            for row, (g,) in zip(b.to_pylist(), geoms, strict=True):
                f.write("\n" if first else ",\n")
                first = False
                props = dumps({k: row[k] for k in names})
                f.write(f'{{"type":"Feature","geometry":{g or "null"},"properties":{props}}}')
        f.write("\n]}\n")
    con.close()
