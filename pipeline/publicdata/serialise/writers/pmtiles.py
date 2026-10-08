from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING

from publicdata.serialise import dumps
from publicdata.serialise.geo import (
    MAXZOOM,
    MVT_TYPES,
    TILE_EXTENT,
    WEB_MERCATOR,
    _connect,
    _crs,
    _with_geometry,
    geo_kind,
)

if TYPE_CHECKING:
    from pathlib import Path


def write_pmtiles(tbl, header: dict, path: Path) -> None:
    """Vector tiles of the layer from zoom 0 to geometry.maxzoom.

    There is one tile layer named by the dataset, and every field is a property. Each zoom's
    shapes are simplified to a tile pixel first, and a shape the publisher drew invalid is
    repaired for the tiles only. A repair can return a collection, such as a polygon with a stray
    line, which a tile cannot hold, so only the parts of the layer's own kind are kept.
    """
    from pmtiles.tile import Compression, TileType, zxy_to_tileid
    from pmtiles.writer import Writer

    ds = tbl.dataset
    maxzoom = int(ds.geometry.get("maxzoom", MAXZOOM))
    con = _connect()
    _with_geometry(con, tbl)
    names = [f.name for f in ds.fields]
    part = 2 if geo_kind(ds) == "line" else 3
    con.execute(
        "CREATE OR REPLACE TABLE f AS SELECT row_number() OVER () AS __id, * EXCLUDE (geometry), "
        f"ST_Transform(ST_CollectionExtract(ST_MakeValid(geometry), {part}), '{_crs(ds)}', 'EPSG:3857', always_xy := true) AS g "
        "FROM t WHERE geometry IS NOT NULL"
    )
    props = ", ".join(f"'{n}': \"{n}\"" for n in names)
    W = WEB_MERCATOR
    tiles: list[tuple[int, bytes]] = []
    for z in range(maxzoom + 1):
        m = 2 * W / 2**z
        rows = con.execute(
            f"""
            WITH s AS (SELECT * EXCLUDE (g), ST_SimplifyPreserveTopology(g, {m / TILE_EXTENT}) AS g FROM f),
            b AS (SELECT *,
                    greatest(0, floor((ST_XMin(g) + {W}) / {m}))::INTEGER AS x0,
                    least({2**z - 1}, floor((ST_XMax(g) + {W}) / {m}))::INTEGER AS x1,
                    greatest(0, floor(({W} - ST_YMax(g)) / {m}))::INTEGER AS y0,
                    least({2**z - 1}, floor(({W} - ST_YMin(g)) / {m}))::INTEGER AS y1
                  FROM s WHERE NOT ST_IsEmpty(g)),
            t AS (SELECT b.*, rx.x::INTEGER AS x, ry.y::INTEGER AS y
                  FROM b, range(x0, x1 + 1) rx(x), range(y0, y1 + 1) ry(y)),
            c AS (SELECT x, y, __id, {", ".join(f'"{n}"' for n in names)},
                    ST_AsMVTGeom(g, ST_Extent(ST_TileEnvelope({z}, x, y)), {TILE_EXTENT}, 64, true) AS geom
                  FROM t)
            SELECT x, y, ST_AsMVT({{{props}, 'geom': geom}}, '{ds.slug}', {TILE_EXTENT}, 'geom' ORDER BY __id)
            FROM c WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
            GROUP BY x, y ORDER BY x, y
            """
        ).fetchall()
        tiles += [(zxy_to_tileid(z, x, y), bytes(tile)) for x, y, tile in rows]
    box = con.execute(
        "WITH w AS (SELECT ST_Transform(geometry, "
        f"'{_crs(ds)}', 'EPSG:4326', always_xy := true) AS g FROM t WHERE geometry IS NOT NULL) "
        "SELECT min(ST_XMin(g)), min(ST_YMin(g)), max(ST_XMax(g)), max(ST_YMax(g)) FROM w"
    ).fetchone()
    con.close()
    tiles.sort(key=lambda x: x[0])
    with path.open("wb") as f:
        w = Writer(f)
        for tid, data in tiles:
            w.write_tile(tid, gzip.compress(data, mtime=0))
        w.finalize(
            {
                "tile_type": TileType.MVT,
                "tile_compression": Compression.GZIP,
                "min_zoom": 0,
                "max_zoom": maxzoom,
                "min_lon_e7": int(box[0] * 1e7),
                "min_lat_e7": int(box[1] * 1e7),
                "max_lon_e7": int(box[2] * 1e7),
                "max_lat_e7": int(box[3] * 1e7),
                "center_zoom": 3,
                "center_lon_e7": int((box[0] + box[2]) / 2 * 1e7),
                "center_lat_e7": int((box[1] + box[3]) / 2 * 1e7),
            },
            {
                "name": ds.title,
                "description": ds.summary or ds.title,
                "attribution": header["attribution"],
                "vector_layers": [
                    {
                        "id": ds.slug,
                        "fields": {f.name: MVT_TYPES.get(f.type, "String") for f in ds.fields},
                        "minzoom": 0,
                        "maxzoom": maxzoom,
                    }
                ],
                "publicdata": json.loads(dumps(header)),
            },
        )
