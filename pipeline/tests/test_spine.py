import dataclasses
import io
import json
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from publicdata import serialise, spine, store
from publicdata.build import build_version, version_key
from publicdata.cache import BuildCache
from publicdata.normalise import normalise
from publicdata.register import LAT_SOURCE, LON_SOURCE, RegisterError, parse
from publicdata.serialise import formats_for
from publicdata.serialise.geo import _connect
from publicdata.serialise.writers.pmtiles import write_pmtiles
from publicdata.site import _faq

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "pipeline" / "tests" / "fixtures" / "store"
REGISTER = ROOT / "register"
ALL = ["sa2", "lga", "suburb", "postcode", "state_electorate", "federal_electorate"]


def crashes_raw(**over):
    raw = yaml.safe_load((REGISTER / "qld-road-crash-locations.yaml").read_text())
    raw.update(over)
    return raw


def crashes(**over):
    return parse(crashes_raw(enrich=ALL, **over), "qld")


def test_enrich_adds_a_code_and_a_name_per_layer_marked_as_the_spines():
    ds = crashes()
    added = [f for f in ds.fields if spine.is_spine(f.source)]
    assert [f.name for f in added] == [
        "sa2_2021_code",
        "sa2_2021_name",
        "lga_2025_code",
        "lga_2025_name",
        "sal_2021_code",
        "sal_2021_name",
        "poa_2021_code",
        "poa_2021_name",
        "sed_2025_code",
        "sed_2025_name",
        "ced_2025_code",
        "ced_2025_name",
    ]
    assert all("not published by the publisher" in f.description for f in added)


def test_enrich_is_refused_without_points_an_unknown_layer_or_a_clash():
    raw = crashes_raw(enrich=["lga"])
    no_geo = {k: v for k, v in raw.items() if k != "geometry"}
    with pytest.raises(RegisterError, match="point geometry"):
        parse(no_geo, "x")
    with pytest.raises(RegisterError, match="not a spine layer"):
        parse(crashes_raw(enrich=["mesh_block"]), "x")
    with pytest.raises(RegisterError, match="twice"):
        parse(crashes_raw(enrich=["lga", "lga"]), "x")
    clash = crashes_raw(enrich=["lga"])
    clash["fields"] = [*clash["fields"], {"name": "lga_2025_name", "source": "Crash_Ref_Number"}]
    with pytest.raises(RegisterError, match="is the spine's"):
        parse(clash, "x")
    same_label = crashes_raw(enrich=["sa2"])
    same_label["fields"] = [
        *same_label["fields"],
        {"name": "area_2021", "source": "Crash_Ref_Number", "label": "SA2 (2021)"},
    ]
    with pytest.raises(RegisterError, match="label is also a publisher field's label"):
        parse(same_label, "x")


def test_a_polygon_layer_declares_its_kind_and_datum_and_no_coordinates():
    raw = yaml.safe_load((REGISTER / "abs-lga-2025.yaml").read_text())
    assert parse(raw, "lga").geometry["kind"] == "polygon"
    with pytest.raises(RegisterError, match="carries its geometry"):
        parse(raw | {"geometry": raw["geometry"] | {"lon": "x"}}, "lga")
    with pytest.raises(RegisterError, match=re.escape("geometry.kind")):
        parse(raw | {"geometry": raw["geometry"] | {"kind": "raster"}}, "lga")
    with pytest.raises(RegisterError, match="datum"):
        parse(raw | {"geometry": {"kind": "polygon"}}, "lga")


def test_shapes_get_geoparquet_and_vector_tiles_and_points_get_geoparquet(monkeypatch):
    monkeypatch.setattr(serialise, "LIMIT", None)
    # A layer's Parquet is GeoParquet already, with its shapes in it.
    assert "pmtiles" in formats_for(10, "polygon")
    assert "geo.parquet" not in formats_for(10, "polygon")
    assert "pmtiles" not in formats_for(10, "point")
    assert "geo.parquet" in formats_for(10, True)
    assert not {"geojson", "gpkg", "geo.parquet"} & set(formats_for(10, ""))


def _built(ds, tmp_path):
    m = store.manifests(FIXTURES, ds.slug)[-1]
    data = store.source_path(FIXTURES, m).read_bytes()
    return build_version(ds, m, data, tmp_path, FIXTURES)


def test_each_point_takes_the_area_it_falls_in_and_blanks_stay_null(tmp_path):
    tbl, out = _built(crashes(), tmp_path)
    t = tbl.table
    lga = t.column("lga_2025_name").to_pylist()
    loc = t.column("loc_local_government_area").to_pylist()
    joined = [(a, b) for a, b in zip(lga, loc, strict=True) if a]
    # The fixture's layers are simplified, so most points, not all, land in an area.
    assert len(joined) > 0.8 * tbl.rows
    # The publisher names the council as text; the spine's 2025 name agrees for most rows.
    agree = sum(1 for a, b in joined if b and a.split(" (")[0].lower() in b.lower())
    assert agree > 0.8 * len(joined)
    lon = t.column("crash_longitude").to_pylist()
    assert all(
        c is None
        for c, x in zip(t.column("sa2_2021_code").to_pylist(), lon, strict=True)
        if x is None
    )
    man = json.loads(
        (
            tmp_path
            / "d"
            / "qld-road-crash-locations"
            / "v"
            / out.manifest.version
            / "manifest.json"
        ).read_text()
    )
    assert [p["dataset"] for p in man["places"]] == [spine.LAYERS[k].slug for k in ALL]
    sch = json.loads(
        (
            tmp_path / "d" / "qld-road-crash-locations" / "v" / out.manifest.version / "schema.json"
        ).read_text()
    )
    f = next(x for x in sch["fields"] if x["name"] == "sal_2021_name")
    assert f["publicdata:derived"]["dataset"] == "abs-suburbs-localities-2021"
    assert (
        f["publicdata:derived"]["version"]
        == store.manifests(FIXTURES, "abs-suburbs-localities-2021")[-1].version
    )
    head = json.loads(
        pq.read_metadata(
            tmp_path
            / "d"
            / "qld-road-crash-locations"
            / "v"
            / out.manifest.version
            / "data.parquet"
        ).metadata[b"publicdata"]
    )
    assert head["places"]["attribution"] == spine.ATTRIBUTION


def test_a_publishers_datum_is_moved_to_gda2020_for_the_join_only(tmp_path):
    ds = crashes()
    gda94 = dataclasses.replace(ds, geometry=ds.geometry | {"crs": "EPSG:4283"})
    a, _ = _built(ds, tmp_path / "a")
    b, _ = _built(gda94, tmp_path / "b")
    # About 1.8 m apart: the published coordinates are untouched and almost every area agrees.
    assert a.table.column("crash_longitude") == b.table.column("crash_longitude")
    same = sum(
        1
        for x, y in zip(
            a.table.column("sal_2021_code").to_pylist(),
            b.table.column("sal_2021_code").to_pylist(),
            strict=True,
        )
        if x == y
    )
    assert same >= a.rows - 3


def test_a_spine_layer_change_is_a_new_cache_entry(tmp_path, monkeypatch):
    ds = crashes()
    cache = BuildCache(tmp_path / "c")
    m = store.manifests(FIXTURES, ds.slug)[-1]
    before = version_key(cache, ds, m, FIXTURES)
    real = store.manifests

    def moved(root, slug):
        ms = real(root, slug)
        if slug == "abs-lga-2025":
            ms[-1] = dataclasses.replace(ms[-1], sha256="0" * 64)
        return ms

    monkeypatch.setattr(store, "manifests", moved)
    assert version_key(cache, ds, m, FIXTURES) != before
    with pytest.raises(ValueError, match="needs the store"):
        version_key(cache, ds, m)


def test_a_missing_layer_stops_the_build(tmp_path):
    ds = crashes()
    empty = tmp_path / "store"
    (empty / ds.slug).mkdir(parents=True)
    m = store.manifests(FIXTURES, ds.slug)[-1]
    with pytest.raises(spine.SpineError, match="has no version"):
        spine.enrich(normalise(ds, m, store.source_path(FIXTURES, m).read_bytes()), empty, REGISTER)


def test_a_polygon_layer_keeps_rows_with_no_shape_and_reads_every_attribute():
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"CODE": "1", "NAME": "A", "AREA": 1.5},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[150, -30], [151, -30], [151, -29], [150, -30]]],
                },
            },
            {
                "type": "Feature",
                "properties": {"CODE": "9", "NAME": "No usual address", "AREA": None},
                "geometry": None,
            },
        ],
    }
    attrs, wkb = spine.read_shapes(json.dumps(fc).encode(), "geojson", "", "EPSG:7844")
    assert attrs.column("CODE").to_pylist() == ["1", "9"]
    assert attrs.column("AREA").to_pylist() == ["1.5", ""]
    assert wkb[0].as_py() is not None
    assert wkb[1].as_py() is None


def test_the_spine_needs_its_extension_installed_not_fetched_at_build(monkeypatch):
    class Con:
        def execute(self, sql):
            return self

        def load_extension(self, name):
            msg = "not installed"
            raise duckdb.IOException(msg)

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: Con())
    with pytest.raises(spine.SpineError, match="spine install"):
        spine.connect()


def test_a_partitioned_polygon_layer_writes_json_partitions_without_point_geojson(tmp_path):
    raw = yaml.safe_load((REGISTER / "abs-lga-2025.yaml").read_text())
    ds = parse(raw | {"partition_by": ["state_name"]}, "lga")
    m = store.manifests(FIXTURES, ds.slug)[-1]
    _, out = build_version(ds, m, store.source_path(FIXTURES, m).read_bytes(), tmp_path, FIXTURES)
    entries = out.partitions["state_name"]
    assert entries
    assert all("geojson" not in e for e in entries)


def test_the_places_question_names_only_the_layers_a_dataset_joins():
    class V:
        manifest = store.manifests(FIXTURES, "qld-road-crash-locations")[-1]
        files = {}
        left_out = {}
        rows = 300

    qs = [q for q, _ in _faq(parse(crashes_raw(enrich=["postcode", "lga"]), "qld"), V, {})]
    assert "Which postcode and council area is each row of Road crash locations in?" in qs


def test_a_point_shapefile_reads_its_coordinates_as_published(tmp_path):
    con = duckdb.connect()
    con.load_extension("spatial")
    out = tmp_path / "shp"
    out.mkdir()
    con.execute(
        "COPY (SELECT 'B1' AS BORE, ST_Point(131.0435, -12.4634) AS geom) "
        f"TO '{out}/bores.shp' (FORMAT GDAL, DRIVER 'ESRI Shapefile', SRS 'EPSG:4283')"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in sorted(out.iterdir()):
            z.write(p, p.name)
    t = spine.read_points(buf.getvalue(), "zip", "bores.shp")
    assert t.column("BORE").to_pylist() == ["B1"]
    assert float(t.column(LON_SOURCE)[0].as_py()) == pytest.approx(131.0435)
    assert float(t.column(LAT_SOURCE)[0].as_py()) == pytest.approx(-12.4634)


def test_a_polygon_whose_repair_leaves_a_stray_line_still_makes_tiles(tmp_path):
    # A ring with a spike: making it valid gives a polygon and a line, which a tile cannot hold.
    spiked = (
        "POLYGON((150 -30, 151 -30, 151 -29, 150.5 -29, 150.5 -28.5, 150.5 -29, 150 -29, 150 -30))"
    )
    wkb = bytes(_connect().execute(f"SELECT ST_AsWKB(ST_GeomFromText('{spiked}'))").fetchone()[0])
    ds = SimpleNamespace(
        slug="t",
        title="T",
        summary="",
        geometry={"kind": "polygon", "maxzoom": 4},
        fields=[SimpleNamespace(name="code", type="string")],
    )
    tbl = SimpleNamespace(
        dataset=ds,
        table=pa.table({"code": ["1"]}),
        geometry=pa.chunked_array([pa.array([wkb], pa.binary())]),
    )
    path = tmp_path / "data.pmtiles"
    write_pmtiles(tbl, {"attribution": "A"}, path)
    assert path.stat().st_size > 0
