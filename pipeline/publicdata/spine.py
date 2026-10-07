"""The place spine: the ABS boundaries every point dataset is joined to by location.

A point dataset that names `enrich` gains, for each layer, the code and name of the area its
point falls in, from the newest version of that layer's own register entry. Points are moved to
GDA2020 (EPSG:7844), the boundaries' datum, for the join only; the published coordinates stay as
the publisher gave them. A point on a shared boundary takes the area with the lowest code, and a
point in no area, or with no coordinates, gets nulls. The layer versions used are recorded in the
version's manifest and schema.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from . import store

DATUM = "EPSG:7844"
EXTENSION_PIN = Path(__file__).with_name("spatial-extension.json")
EXTENSION_BUCKET = "publicdata-raw"
EXTENSION_REPOSITORY = "https://extensions.duckdb.org"
MEMORY_LIMIT = "3GB"
SOURCE_PREFIX = "(spine: "
ATTRIBUTION = (
    "Place columns from Australian Statistical Geography Standard (ASGS) Edition 3 boundaries, "
    "Australian Bureau of Statistics, licensed under CC BY 4.0."
)
NOTE = (
    "Joined by location: the area of the {layer} that the row's point falls in, from version "
    "{version} of {slug}. Derived by publicdata.au, not by the publisher."
)


@dataclass(frozen=True)
class Layer:
    key: str
    slug: str
    title: str
    # (field name, label) for the code and the name, as the layer's own entry names them.
    code: tuple[str, str]
    name: tuple[str, str]
    # What a reader calls one area of the layer.
    noun: str


LAYERS = {
    layer.key: layer
    for layer in (
        Layer(
            "sa2",
            "abs-sa2-2021",
            "Statistical Area Level 2 (2021)",
            ("sa2_2021_code", "SA2 code (2021)"),
            ("sa2_2021_name", "SA2 (2021)"),
            "SA2",
        ),
        Layer(
            "lga",
            "abs-lga-2025",
            "Local Government Area (2025)",
            ("lga_2025_code", "Council area code (LGA 2025)"),
            ("lga_2025_name", "Council area (LGA 2025)"),
            "council area",
        ),
        Layer(
            "suburb",
            "abs-suburbs-localities-2021",
            "Suburb and Locality (2021)",
            ("sal_2021_code", "Suburb or locality code (2021)"),
            ("sal_2021_name", "Suburb or locality (2021)"),
            "suburb",
        ),
        Layer(
            "postcode",
            "abs-postal-areas-2021",
            "Postal Area (2021)",
            ("poa_2021_code", "Postal area code (2021)"),
            ("poa_2021_name", "Postcode (postal area 2021)"),
            "postcode",
        ),
        Layer(
            "state_electorate",
            "abs-state-electoral-divisions-2025",
            "State Electoral Division (2025)",
            ("sed_2025_code", "State electorate code (2025)"),
            ("sed_2025_name", "State electorate (2025)"),
            "state electorate",
        ),
        Layer(
            "federal_electorate",
            "abs-federal-electoral-divisions-2025",
            "Commonwealth Electoral Division (2025)",
            ("ced_2025_code", "Federal electorate code (2025)"),
            ("ced_2025_name", "Federal electorate (2025)"),
            "federal electorate",
        ),
    )
}


class SpineError(RuntimeError):
    pass


def is_spine(source: str) -> bool:
    return source.startswith(SOURCE_PREFIX)


def _pin(path: Path = EXTENSION_PIN) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def footer(path: Path) -> dict:
    """The build, DuckDB release and platform a DuckDB extension file declares in its footer,
    which DuckDB itself reads (and signs) before it loads one."""
    with path.open("rb") as f:
        f.seek(-512, os.SEEK_END)
        meta = f.read(256)
    fields = [meta[i : i + 32].rstrip(b"\0").decode() for i in range(0, 256, 32)][::-1]
    return {"platform": fields[1], "duckdb": fields[2].removeprefix("v"), "build": fields[3]}


def _gunzip(src: Path, dest: Path) -> Path:
    with gzip.open(src, "rb") as f, dest.open("wb") as out:
        shutil.copyfileobj(f, out, 1 << 20)
    return dest


def _download(url: str, dest: Path) -> None:
    import requests

    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def _r2_credentials() -> bool:
    return all(
        os.environ.get(k)
        for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_ID", "CLOUDFLARE_ACCOUNT_ID")
    )


def install() -> None:
    """Install the spatial extension build pinned in spatial-extension.json, checked against its
    SHA-256 and DuckDB release, once, so that a build only loads it and stays offline. A runner
    with the R2 credentials takes our copy; anyone else takes the same bytes from DuckDB."""
    import duckdb

    pin = _pin()
    if pin["duckdb"] != duckdb.__version__:
        raise SpineError(
            f"{EXTENSION_PIN.name} pins the spatial extension for DuckDB {pin['duckdb']}, and "
            f"DuckDB {duckdb.__version__} is installed; run the Spatial extension workflow"
        )
    with tempfile.TemporaryDirectory() as tmp:
        gz = Path(tmp) / "spatial.duckdb_extension.gz"
        if _r2_credentials():
            from .r2 import client

            bucket, key = pin["url"].removeprefix("r2://").split("/", 1)
            client().download_file(bucket, key, str(gz))
            print(f"spine: fetched {pin['url']}")
        else:
            _download(pin["upstream"], gz)
            print(f"spine: fetched {pin['upstream']}")
        got = store.sha256_file(gz)
        if got != pin["sha256"]:
            raise SpineError(
                f"the spatial extension fetched has SHA-256 {got}, and {EXTENSION_PIN.name} pins "
                f"{pin['sha256']}"
            )
        ext = _gunzip(gz, Path(tmp) / "spatial.duckdb_extension")
        meta = footer(ext)
        want = {k: pin[k] for k in meta}
        if meta != want:
            raise SpineError(f"the spatial extension fetched is {meta}, not {want}")
        duckdb.connect().install_extension(str(ext), force_install=True)


def mirror(pin_path: Path = EXTENSION_PIN) -> dict:
    """Copy the spatial extension DuckDB serves for the installed release to R2 and pin it,
    after checking the download is byte for byte what DuckDB's own INSTALL fetches and loads."""
    import duckdb
    from botocore.exceptions import ClientError

    from .r2 import client

    con = duckdb.connect()
    platform = con.execute("PRAGMA platform").fetchone()[0]
    release = duckdb.__version__
    upstream = f"{EXTENSION_REPOSITORY}/v{release}/{platform}/spatial.duckdb_extension.gz"
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        gz = t / "spatial.duckdb_extension.gz"
        _download(upstream, gz)
        ext = _gunzip(gz, t / "spatial.duckdb_extension")
        own = duckdb.connect(config={"extension_directory": str(t / "duckdb")})
        own.install_extension("spatial")
        own.load_extension("spatial")
        (installed,) = own.execute(
            "SELECT install_path FROM duckdb_extensions() WHERE extension_name = 'spatial'"
        ).fetchone()
        if store.sha256_file(ext) != store.sha256_file(Path(installed)):
            raise SpineError(f"{upstream} is not the build DuckDB's own INSTALL fetched")
        meta = footer(ext)
        if (meta["duckdb"], meta["platform"]) != (release, platform) or not re.fullmatch(
            r"[0-9a-f]{7,40}", meta["build"]
        ):
            raise SpineError(f"{upstream} declares {meta}")
        sha = store.sha256_file(gz)
        key = f"_toolchain/duckdb/v{release}/{platform}/{meta['build']}/spatial.duckdb_extension.gz"
        s3 = client()
        try:
            held = s3.head_object(Bucket=EXTENSION_BUCKET, Key=key)["Metadata"].get("sha256", "")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey"):
                raise
            held = None
        if held is None:
            s3.upload_file(
                str(gz),
                EXTENSION_BUCKET,
                key,
                ExtraArgs={"ContentType": "application/gzip", "Metadata": {"sha256": sha}},
            )
            print(f"spine: put {EXTENSION_BUCKET}/{key}")
        elif held != sha:
            raise SpineError(f"{EXTENSION_BUCKET}/{key} holds other bytes ({held}); not replaced")
    pin = {
        "duckdb": release,
        "platform": platform,
        "build": meta["build"],
        "url": f"r2://{EXTENSION_BUCKET}/{key}",
        "upstream": upstream,
        "sha256": sha,
    }
    pin_path.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
    return pin


def connect():
    import duckdb

    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    # Each connection is held to a share of the machine, since the build opens several in turn
    # and DuckDB would otherwise claim most of the memory for each.
    con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
    try:
        con.load_extension("spatial")
    except duckdb.Error as e:
        raise SpineError(
            "DuckDB's spatial extension is not installed; run `python -m publicdata spine install`"
        ) from e
    return con


def _shape_path(data: bytes, ext: str, member: str, tmp: Path) -> str:
    """A path GDAL can open: the publisher's zip read in place, or the file itself."""
    src = tmp / f"source.{ext}"
    src.write_bytes(data)
    if ext != "zip":
        return str(src)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.lower().endswith((".shp", ".gpkg", ".geojson"))]
    name = member or (names[0] if len(names) == 1 else "")
    if not name or name not in names:
        raise SpineError(f"name the layer's file in source.member, one of {names}")
    return f"/vsizip/{src}/{name}"


def _read_layer(data: bytes, ext: str, member: str):
    """The layer loaded into DuckDB as `src`: the connection, the geometry column, the attribute
    columns cast to text, and the file's order."""
    con = connect()
    with tempfile.TemporaryDirectory() as t:
        path = _shape_path(data, ext, member, Path(t))
        con.execute(f"CREATE TABLE src AS SELECT * FROM ST_Read('{path}')")
    described = con.execute("DESCRIBE src").fetchall()
    cols = [r[0] for r in described if not r[1].startswith("GEOMETRY") and r[0] != "OGC_FID"]
    geom = next(r[0] for r in described if r[1].startswith("GEOMETRY"))
    order = "ORDER BY OGC_FID" if "OGC_FID" in {r[0] for r in described} else ""
    text = ", ".join(f'coalesce(CAST("{c}" AS VARCHAR), \'\') AS "{c}"' for c in cols)
    return con, geom, text, order


def read_shapes(data: bytes, ext: str, member: str, crs: str) -> tuple[pa.Table, pa.Array]:
    """The layer's attributes as text, in the file's order, and each feature's geometry as WKB in
    GDA2020. The publisher's datum is the register's `geometry.crs`."""
    con, geom, text, order = _read_layer(data, ext, member)
    moved = (
        f'"{geom}"'
        if crs == DATUM
        else f"ST_Transform(\"{geom}\", '{crs}', '{DATUM}', always_xy := true)"
    )
    out = con.execute(
        f"SELECT {text}, ST_AsWKB({moved}) AS __wkb FROM src {order}"
    ).to_arrow_table()
    con.close()
    wkb = out.column("__wkb").combine_chunks().cast(pa.binary())
    return out.drop(["__wkb"]), wkb


def read_points(data: bytes, ext: str, member: str) -> pa.Table:
    """A point layer's attributes as text and each point's coordinates as published, under the
    longitude and latitude source names a GeoJSON point is read with."""
    from .register import LAT_SOURCE, LON_SOURCE

    con, geom, text, order = _read_layer(data, ext, member)
    xy = (
        f'coalesce(CAST(ST_X("{geom}") AS VARCHAR), \'\') AS "{LON_SOURCE}", '
        f'coalesce(CAST(ST_Y("{geom}") AS VARCHAR), \'\') AS "{LAT_SOURCE}"'
    )
    out = con.execute(f"SELECT {text}, {xy} FROM src {order}").to_arrow_table()
    con.close()
    return out


@dataclass
class Shapes:
    layer: Layer
    version: str
    sha256: str
    codes: list[str]
    names: list[str]
    wkb: pa.Array


_LOADED: dict[tuple[str, str], Shapes] = {}


def layer_shapes(layer: Layer, store_dir: Path, register_dir: Path) -> Shapes:
    """The newest version of the layer's entry in the store, normalised, read once per build."""
    from .normalise import normalise
    from .register import load

    ms = store.manifests(store_dir, layer.slug)
    if not ms:
        raise SpineError(f"the place spine needs {layer.slug} in the store, and it has no version")
    m = ms[-1]
    key = (layer.slug, m.sha256)
    if key not in _LOADED:
        ds = next((d for d in load(register_dir) if d.slug == layer.slug), None)
        if ds is None:
            raise SpineError(f"{layer.slug} is not in the register")
        tbl = normalise(ds, m, store.source_path(store_dir, m).read_bytes())
        _LOADED[key] = Shapes(
            layer,
            m.version,
            m.sha256,
            tbl.table.column(layer.code[0]).to_pylist(),
            tbl.table.column(layer.name[0]).to_pylist(),
            tbl.geometry,
        )
    return _LOADED[key]


_ENTRIES: dict[tuple[str, bytes], str] = {}


def _layer_entry(slug: str, register_dir: Path) -> str:
    """The layer's register entry as a version key reads it, its rebuild number among it."""
    import yaml

    from .cache import entry_key
    from .register import parse

    p = register_dir / f"{slug}.yaml"
    if not p.is_file():
        p = next(iter(sorted(register_dir.rglob(f"{slug}.yaml"))), None)
        if p is None:
            return "missing"
    raw = p.read_bytes()
    if (slug, raw) not in _ENTRIES:
        ds = parse(yaml.safe_load(raw.decode("utf-8")) or {}, p.name)
        _ENTRIES[(slug, raw)] = hashlib.sha256(entry_key(ds).encode()).hexdigest()
    return _ENTRIES[(slug, raw)]


def spine_versions(keys: tuple[str, ...], store_dir: Path, register_dir: Path) -> str:
    """What the join reads, for the build cache: each layer's newest source hash and the register
    entry the layer is normalised with, so a change to either rebuilds the datasets joined to it."""
    parts = []
    for k in keys:
        slug = LAYERS[k].slug
        ms = store.manifests(store_dir, slug)
        parts.append(
            f"{slug}@{ms[-1].sha256 if ms else 'missing'}@{_layer_entry(slug, register_dir)}"
        )
    return "|".join(parts)


def enrich(tbl, store_dir: Path, register_dir: Path):
    """The table with each spine column filled in by location. Returns the layers used."""
    ds = tbl.dataset
    g = ds.geometry or {}
    con = connect()
    lon = tbl.table.column(g["lon"]).combine_chunks().cast(pa.float64())
    lat = tbl.table.column(g["lat"]).combine_chunks().cast(pa.float64())
    pts = pa.table({"i": pa.array(range(tbl.rows), pa.int64()), "x": lon, "y": lat})
    con.register("pts_in", pts)
    crs = str(g.get("crs") or DATUM)
    point = "ST_Point(x, y)"
    if crs != DATUM:
        point = f"ST_Transform({point}, '{crs}', '{DATUM}', always_xy := true)"
    con.execute(
        f"CREATE TABLE pts AS SELECT i, {point} AS p FROM pts_in WHERE x IS NOT NULL AND y IS NOT NULL"
    )
    used = []
    cols = {}
    for key in ds.enrich:
        layer = LAYERS[key]
        sh = layer_shapes(layer, store_dir, register_dir)
        con.register("shape_in", pa.table({"code": sh.codes, "name": sh.names, "wkb": sh.wkb}))
        con.execute(
            "CREATE OR REPLACE TABLE shape AS SELECT code, name, ST_MakeValid(ST_GeomFromWKB(wkb)) AS g "
            "FROM shape_in WHERE wkb IS NOT NULL"
        )
        con.execute("CREATE INDEX shape_g ON shape USING RTREE (g)")
        # Every row in order, with the area its point falls in or nulls, built inside DuckDB so a
        # table of millions of rows never becomes Python objects.
        hit = con.execute(
            "WITH h AS (SELECT i, min(code) AS code, arg_min(name, code) AS name FROM pts "
            "JOIN shape ON ST_Intersects(shape.g, pts.p) GROUP BY i) "
            "SELECT h.code, h.name FROM pts_in LEFT JOIN h USING (i) ORDER BY pts_in.i"
        ).to_arrow_table()
        con.unregister("shape_in")
        cols[layer.code[0]] = hit.column("code").combine_chunks().cast(pa.string())
        cols[layer.name[0]] = hit.column("name").combine_chunks().cast(pa.string())
        used.append(
            {"layer": key, "dataset": layer.slug, "version": sh.version, "sha256": sh.sha256}
        )
    con.close()
    t = tbl.table
    at = t.column_names.index("suppressed") if "suppressed" in t.column_names else t.num_columns
    for f in ds.fields:
        if is_spine(f.source):
            t = t.add_column(at, f.name, cols[f.name])
            at += 1
    tbl.table = t
    tbl.places = used
    return tbl
