import dataclasses
import datetime as dt
import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Never, Self, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from pmtiles.reader import MemorySource, Reader, all_tiles

from publicdata import fetch, serialise, spine, store
from publicdata.build import as_fetched, build_dataset, build_version, version_key, version_keys
from publicdata.cache import BuildCache
from publicdata.normalise import Table, normalise
from publicdata.register import LAT_SOURCE, LON_SOURCE, Field, RegisterError, parse
from publicdata.serialise import formats_for
from publicdata.serialise.geo import _connect
from publicdata.serialise.writers.pmtiles import write_pmtiles
from publicdata.site import _faq

from .conftest import make_dataset, make_header, make_manifest, present

if TYPE_CHECKING:
    from typing import Unpack

    from publicdata.build import VersionOut
    from publicdata.fetch import LicenceRead
    from publicdata.register import Dataset, Geometry, RawEntry

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "pipeline" / "tests" / "fixtures" / "store"
REGISTER = ROOT / "register"
ALL = ["sa2", "lga", "suburb", "postcode", "state_electorate", "federal_electorate"]


def crashes_raw(**over: Unpack[RawEntry]) -> RawEntry:
    raw: RawEntry = yaml.safe_load((REGISTER / "qld-road-crash-locations.yaml").read_text())
    raw.update(over)
    return raw


def crashes(**over: Unpack[RawEntry]) -> Dataset:
    joined: RawEntry = {"enrich": [*ALL]}
    return parse(crashes_raw(**(joined | over)), "qld")


def test_enrich_adds_a_code_and_a_name_per_layer_marked_as_the_spines() -> None:
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


def test_enrich_is_refused_without_points_an_unknown_layer_or_a_clash() -> None:
    raw = crashes_raw(enrich=["lga"])
    no_geo = raw.copy()
    del no_geo["geometry"]
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


def test_a_polygon_layer_declares_its_kind_and_datum_and_no_coordinates() -> None:
    text = (REGISTER / "abs-lga-2025.yaml").read_text()
    raw: RawEntry = yaml.safe_load(text)
    geometry: Geometry = yaml.safe_load(text)["geometry"]
    assert present(parse(raw, "lga").geometry)["kind"] == "polygon"
    with_lon: Geometry = {**geometry, "lon": "x"}
    with pytest.raises(RegisterError, match="carries its geometry"):
        parse(raw | {"geometry": with_lon}, "lga")
    raster: Geometry = {**geometry, "kind": "raster"}
    with pytest.raises(RegisterError, match=re.escape("geometry.kind")):
        parse(raw | {"geometry": raster}, "lga")
    # An entry that leaves out the datum, which the register refuses.
    no_datum = cast("Geometry", {"kind": "polygon"})
    with pytest.raises(RegisterError, match="datum"):
        parse(raw | {"geometry": no_datum}, "lga")


def test_shapes_get_geoparquet_and_vector_tiles_and_points_get_geoparquet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serialise, "LIMIT", None)
    # A layer's Parquet is GeoParquet already, with its shapes in it.
    assert "pmtiles" in formats_for(10, "polygon")
    assert "geo.parquet" not in formats_for(10, "polygon")
    assert "pmtiles" not in formats_for(10, "point")
    assert "geo.parquet" in formats_for(10, geometry=True)
    assert not {"geojson", "gpkg", "geo.parquet"} & set(formats_for(10, ""))


def _built(ds: Dataset, tmp_path: Path) -> tuple[Table, VersionOut]:
    m = store.manifests(FIXTURES, ds.slug)[-1]
    data = store.source_path(FIXTURES, m).read_bytes()
    return build_version(ds, m, data, tmp_path, FIXTURES)


def test_each_point_takes_the_area_it_falls_in_and_blanks_stay_null(tmp_path: Path) -> None:
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
    meta = pq.read_metadata(
        tmp_path / "d" / "qld-road-crash-locations" / "v" / out.manifest.version / "data.parquet"
    ).metadata
    head = json.loads(present(meta)[b"publicdata"])
    assert head["places"]["attribution"] == spine.ATTRIBUTION


def test_a_publishers_datum_is_moved_to_gda2020_for_the_join_only(tmp_path: Path) -> None:
    ds = crashes()
    gda94 = dataclasses.replace(ds, geometry=present(ds.geometry) | {"crs": "EPSG:4283"})
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


def later_layer(st: Path, slug: str = "abs-lga-2025") -> store.Manifest:
    """A newer version of a layer in the store, as a later fetch of the ABS file would add."""
    m = dataclasses.replace(
        store.manifests(FIXTURES, slug)[-1], version="2027-01-01", sha256="1" * 64
    )
    d = store.version_dir(st, slug, m.version)
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(m.to_json(), encoding="utf-8")
    return m


def spine_copy(tmp: Path) -> Path:
    """The fixture store, which a test may add layer versions to."""
    st = tmp / "store"
    for d in FIXTURES.iterdir():
        if d.name.startswith("abs-") or d.name == "qld-road-crash-locations":
            shutil.copytree(d, st / d.name)
    return st


def test_a_new_layer_version_leaves_a_joined_version_as_it_was(tmp_path: Path) -> None:
    ds = crashes()
    st = spine_copy(tmp_path)
    cache = BuildCache(tmp_path / "c")
    m = store.manifests(st, ds.slug)[-1]
    before = version_key(cache, ds, m, st)
    pinned = next(p for p in m.spine if p["layer"] == "lga")
    later_layer(st)
    assert version_key(cache, ds, m, st) == before
    tbl = spine.enrich(normalise(ds, m, store.source_path(st, m).read_bytes()), st, REGISTER)
    assert next(p for p in tbl.places if p["layer"] == "lga")["version"] == pinned["version"]
    with pytest.raises(ValueError, match="needs the store"):
        version_key(cache, ds, m)


def test_an_enrich_edit_leaves_a_joined_version_as_it_was(tmp_path: Path) -> None:
    ds = crashes()
    cache = BuildCache(tmp_path / "c")
    m = store.manifests(FIXTURES, ds.slug)[-1]
    before = version_key(cache, ds, m, FIXTURES)
    edited = crashes(enrich=["lga"])
    assert version_key(cache, edited, m, FIXTURES) == before
    assert as_fetched(edited, m) == ds
    assert as_fetched(crashes(enrich=[]), m) == ds


def test_a_new_fetch_pins_the_newest_version_of_each_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = crashes(enrich=["lga", "sa2"])
    st = spine_copy(tmp_path)
    shutil.rmtree(st / ds.slug)
    newer = later_layer(st)
    held = store.manifests(FIXTURES, ds.slug)[-1]
    data = store.source_path(FIXTURES, held).read_bytes()
    m = dataclasses.replace(held, spine=[], version="2027-02-01", sha256="2" * 64)
    lic: LicenceRead = {"id": ds.licence.id, "read_from": "https://e", "read_at": "2027-02-01"}
    monkeypatch.setitem(fetch.ADAPTERS, ds.source.adapter, lambda d, s: (data, m, lic))
    monkeypatch.setattr(fetch, "check_licence", lambda d, lic: None)
    got = present(fetch.fetch(ds, st))
    sa2 = store.manifests(st, "abs-sa2-2021")[-1]
    assert got.spine == [
        {
            "layer": "lga",
            "dataset": "abs-lga-2025",
            "version": newer.version,
            "sha256": newer.sha256,
        },
        {"layer": "sa2", "dataset": "abs-sa2-2021", "version": sa2.version, "sha256": sa2.sha256},
    ]
    assert store.manifests(st, ds.slug)[-1].spine == got.spine


def test_a_fetch_with_no_pin_after_a_pinned_one_is_refused(tmp_path: Path) -> None:
    ds = crashes()
    st = spine_copy(tmp_path)
    m = store.manifests(st, ds.slug)[-1]
    later = dataclasses.replace(m, version="2026-05-01", spine=[])
    store.write(st, later, store.source_path(st, m).read_bytes())
    with pytest.raises(spine.SpineError, match="2026-05-01: fetched after a pinned version"):
        version_keys(BuildCache(tmp_path / "c"), ds, st)
    with pytest.raises(spine.SpineError, match="fetched after a pinned version"):
        build_dataset(ds, st, tmp_path / "out")


def test_a_version_fetched_before_its_dataset_was_joined_is_built_unjoined(tmp_path: Path) -> None:
    joined = crashes()
    plain = crashes(enrich=[])
    m = dataclasses.replace(store.manifests(FIXTURES, joined.slug)[-1], spine=[])
    assert as_fetched(joined, m) == plain
    cache = BuildCache(tmp_path / "c")
    assert version_key(cache, joined, m, FIXTURES) == version_key(cache, plain, m, FIXTURES)
    with pytest.raises(spine.SpineError, match="pins no place spine layers"):
        spine.spine_versions(m, REGISTER)


def test_a_pinned_layer_whose_field_the_publisher_now_uses_fails_loudly() -> None:
    raw = crashes_raw(enrich=["sa2"])
    raw["fields"] = [*raw["fields"], {"name": "lga_2025_code", "source": "Crash_Ref_Number"}]
    ds = parse(raw, "qld")
    m = store.manifests(FIXTURES, ds.slug)[-1]
    with pytest.raises(ValueError, match=r"\['lga_2025_code'\] are the spine's it pins"):
        as_fetched(ds, m)


def test_a_changed_pin_is_a_new_cache_entry(tmp_path: Path) -> None:
    ds = crashes()
    cache = BuildCache(tmp_path / "c")
    m = store.manifests(FIXTURES, ds.slug)[-1]
    moved = [p.copy() for p in m.spine]
    moved[1]["sha256"] = "1" * 64
    assert version_key(cache, ds, dataclasses.replace(m, spine=moved), FIXTURES) != version_key(
        cache, ds, m, FIXTURES
    )


def test_a_pin_whose_hash_differs_from_the_stored_layer_is_refused() -> None:
    p = next(p for p in store.manifests(FIXTURES, "qld-road-crash-locations")[-1].spine)
    with pytest.raises(spine.SpineError, match="is not there"):
        spine.layer_shapes({**p, "sha256": "1" * 64}, FIXTURES, REGISTER)


def test_the_key_reads_a_pinned_manifest_as_it_was_before_the_pin() -> None:
    raw = (ROOT / "store" / "vic-road-crashes" / "2026-09-15" / "manifest.json").read_text()
    m = store.Manifest.read(ROOT / "store" / "vic-road-crashes" / "2026-09-15" / "manifest.json")
    assert m.spine
    before = {k: v for k, v in json.loads(raw).items() if k != "spine"}
    assert m.keyed_json() == json.dumps(before, indent=2, ensure_ascii=False) + "\n"


def test_a_fetch_run_pins_only_the_layer_versions_main_held_when_it_began(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = crashes(enrich=["lga"])
    st = spine_copy(tmp_path)
    shutil.rmtree(st / ds.slug)
    held = store.manifests(st, "abs-lga-2025")[-1]
    monkeypatch.setattr(fetch, "SPINE", spine.committed(st))
    later_layer(st)
    src = store.manifests(FIXTURES, ds.slug)[-1]
    m = dataclasses.replace(src, spine=[], version="2027-02-01", sha256="2" * 64)
    lic: LicenceRead = {"id": ds.licence.id, "read_from": "https://e", "read_at": "2027-02-01"}
    data = store.source_path(FIXTURES, src).read_bytes()
    monkeypatch.setitem(fetch.ADAPTERS, ds.source.adapter, lambda d, s: (data, m, lic))
    monkeypatch.setattr(fetch, "check_licence", lambda d, lic: None)
    got = present(fetch.fetch(ds, st))
    assert [p["version"] for p in got.spine] == [held.version]


def test_a_rolling_fetch_pins_its_layers(tmp_path: Path) -> None:
    ds = crashes(enrich=["lga"], update="rolling")
    st = spine_copy(tmp_path)
    shutil.rmtree(st / ds.slug)
    newer = later_layer(st)
    src = store.manifests(FIXTURES, ds.slug)[-1]
    data = store.source_path(FIXTURES, src).read_bytes()
    lic: LicenceRead = {"id": ds.licence.id, "read_from": "https://e", "read_at": "2027-02-01"}
    m = dataclasses.replace(src, spine=[])
    got = present(fetch.fetch_rolling(ds, st, data, m, lic, dt.date(2027, 2, 1)))
    assert [(p["layer"], p["version"]) for p in got.spine] == [("lga", newer.version)]


def test_each_version_is_built_with_the_layers_its_fetch_pinned(tmp_path: Path) -> None:
    st = spine_copy(tmp_path)
    old = store.manifests(st, "qld-road-crash-locations")[-1]
    data = store.source_path(st, old).read_bytes()
    two = [p for p in old.spine if p["layer"] in ("lga", "sa2")]
    new = dataclasses.replace(old, version="2026-05-01", spine=two)
    store.write(st, new, data)
    ds = crashes(enrich=["lga"])
    _tbl, vout = build_version(ds, old, data, tmp_path / "direct", st)
    names = [f["name"] for f in vout_schema(tmp_path / "direct", old)["fields"]]
    assert "sal_2021_code" in names
    assert "ced_2025_code" in names
    out = tmp_path / "out"
    dout = build_dataset(ds, st, out)
    assert dout.dataset.enrich == ("sa2", "lga")
    ddir = out / "d" / ds.slug
    per = {
        v["version"]: v["fields"]
        for v in json.loads((ddir / "versions.json").read_text())["versions"]
    }
    base = len([f for f in ds.fields if not spine.is_spine(f.source)])
    assert per == {old.version: base + 12, new.version: base + 4}
    newest = [f["name"] for f in json.loads((ddir / "schema.json").read_text())["fields"]]
    assert "sa2_2021_code" in newest
    assert "sal_2021_code" not in newest
    assert vout.rows > 0


def vout_schema(out: Path, m: store.Manifest) -> dict[str, list[dict[str, str]]]:
    return cast(
        "dict[str, list[dict[str, str]]]",
        json.loads((out / "d" / m.dataset / "v" / m.version / "schema.json").read_text()),
    )


def test_a_build_pulls_the_layer_versions_its_versions_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from publicdata import __main__ as cli  # noqa: PLC0415 - the CLI's helpers, tested alone
    from publicdata import build  # noqa: PLC0415 - patched for this test

    st = spine_copy(tmp_path)
    pinned = store.manifests(st, "abs-lga-2025")[-1]
    newer = later_layer(st)
    # A dataset whose entry joins nothing still pulls what its stored versions pinned.
    births = dataclasses.replace(
        store.manifests(st, "qld-road-crash-locations")[-1], dataset="au-births-by-state"
    )
    d = store.version_dir(st, "au-births-by-state", births.version)
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(births.to_json(), encoding="utf-8")
    assert "abs-lga-2025" in cli._with_layers(["au-births-by-state"], st)
    # The joined crash version is not cached, so the layer version it pinned is pulled, and the
    # newer one it never read is not.
    monkeypatch.setattr(
        build, "version_keys", lambda c, ds, s: [(m, ds.slug) for m in store.manifests(s, ds.slug)]
    )
    monkeypatch.setattr(build, "newest_fetch", lambda ds, s: None)
    monkeypatch.setattr(BuildCache, "has", lambda self, k: k != "qld-road-crash-locations")
    cached = cli._cached_versions(st, tmp_path / "c")
    assert ("abs-lga-2025", pinned.version) not in cached
    assert ("abs-lga-2025", newer.version) in cached


def test_every_pin_names_a_committed_layer_version_with_its_hash() -> None:
    held = {
        (m.dataset, m.version): m.sha256
        for layer in spine.LAYERS.values()
        for m in store.manifests(ROOT / "store", layer.slug, fetches=True)
    }
    wrong = [
        f"{d.name} {m.version} {p['dataset']}@{p['version']}"
        for d in (ROOT / "store").iterdir()
        if d.is_dir()
        for m in store.manifests(ROOT / "store", d.name, fetches=True)
        for p in m.spine
        if held.get((p["dataset"], p["version"])) != p["sha256"]
    ]
    assert wrong == []


def test_no_stored_dataset_has_a_fetch_without_a_pin_after_a_pinned_one() -> None:
    gaps = [
        g
        for d in (ROOT / "store").iterdir()
        if d.is_dir()
        for g in spine.pinned_after(store.manifests(ROOT / "store", d.name, fetches=True))
    ]
    assert gaps == []
    joined = {"vic-road-crashes", "qld-road-crash-locations", "tas-road-crashes"}
    assert all(store.manifests(ROOT / "store", s)[-1].spine for s in joined)


def test_a_missing_layer_stops_the_build(tmp_path: Path) -> None:
    ds = crashes()
    empty = tmp_path / "store"
    (empty / ds.slug).mkdir(parents=True)
    m = store.manifests(FIXTURES, ds.slug)[-1]
    with pytest.raises(spine.SpineError, match="is not there"):
        spine.enrich(normalise(ds, m, store.source_path(FIXTURES, m).read_bytes()), empty, REGISTER)


def test_every_layer_a_pin_names_is_still_a_spine_layer() -> None:
    named = {
        (p["layer"], p["dataset"])
        for d in (ROOT / "store").iterdir()
        if d.is_dir()
        for m in store.manifests(ROOT / "store", d.name, fetches=True)
        for p in m.spine
    }
    assert named
    assert {(k, spine.LAYERS[k].slug) for k, _ in named if k in spine.LAYERS} == named


def test_a_polygon_layer_keeps_rows_with_no_shape_and_reads_every_attribute() -> None:
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


def test_the_spine_needs_its_extension_installed_not_fetched_at_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Con:
        def execute(self, sql: str) -> Self:
            return self

        def load_extension(self, name: str) -> Never:
            msg = "not installed"
            raise duckdb.IOException(msg)

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: Con())
    with pytest.raises(spine.SpineError, match="spine install"):
        spine.connect()


def test_a_partitioned_polygon_layer_writes_json_partitions_without_point_geojson(
    tmp_path: Path,
) -> None:
    raw: RawEntry = yaml.safe_load((REGISTER / "abs-lga-2025.yaml").read_text())
    ds = parse(raw | {"partition_by": ["state_name"]}, "lga")
    m = store.manifests(FIXTURES, ds.slug)[-1]
    _, out = build_version(ds, m, store.source_path(FIXTURES, m).read_bytes(), tmp_path, FIXTURES)
    entries = out.partitions["state_name"]
    assert entries
    assert all("geojson" not in e for e in entries)


def test_the_places_question_names_only_the_layers_a_dataset_joins() -> None:
    class V:
        manifest = store.manifests(FIXTURES, "qld-road-crash-locations")[-1]
        files: ClassVar[dict[str, int]] = {}
        left_out: ClassVar[dict[str, str]] = {}
        rows = 300
        whole = True

    ds = parse(crashes_raw(enrich=["postcode", "lga"]), "qld")
    qs = [q for q, _ in _faq(ds, V, {})]  # type: ignore[arg-type]  # a VersionOut stand-in
    assert "Which postcode and council area is each row of Road crash locations in?" in qs


def test_a_point_shapefile_reads_its_coordinates_as_published(tmp_path: Path) -> None:
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


def test_a_polygon_whose_repair_leaves_a_stray_line_still_makes_tiles(tmp_path: Path) -> None:
    # A ring with a spike: making it valid gives a polygon and a line, which a tile cannot hold.
    spiked = (
        "POLYGON((150 -30, 151 -30, 151 -29, 150.5 -29, 150.5 -28.5, 150.5 -29, 150 -29, 150 -30))"
    )
    row = _connect().execute(f"SELECT ST_AsWKB(ST_GeomFromText('{spiked}'))").fetchone()
    wkb = bytes(present(row)[0])
    ds = make_dataset(
        [Field("code", "code")],
        title="T",
        geometry={"kind": "polygon", "crs": "EPSG:7844", "maxzoom": 4},
    )
    tbl = Table(
        dataset=ds,
        manifest=make_manifest(b""),
        table=pa.table({"code": ["1"]}),
        geometry=pa.array([wkb], pa.binary()),
    )
    path = tmp_path / "data.pmtiles"
    write_pmtiles(tbl, make_header(1, "data.pmtiles"), path)
    assert path.stat().st_size > 0


def test_a_layer_with_no_shapes_makes_an_empty_archive(tmp_path: Path) -> None:
    ds = make_dataset(
        [Field("code", "code")],
        title="T",
        geometry={"kind": "polygon", "crs": "EPSG:7844", "maxzoom": 4},
    )
    tbl = Table(
        dataset=ds,
        manifest=make_manifest(b""),
        table=pa.table({"code": ["1", "2"]}),
        geometry=pa.array([None, None], pa.binary()),
    )
    paths = [tmp_path / "a.pmtiles", tmp_path / "b.pmtiles"]
    for path in paths:
        write_pmtiles(tbl, make_header(2, "data.pmtiles"), path)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    reader = Reader(MemorySource(paths[0].read_bytes()))
    head = reader.header()
    assert head["tile_entries_count"] == 0
    assert (head["min_zoom"], head["max_zoom"]) == (0, 4)
    assert (head["min_lon_e7"], head["max_lon_e7"]) == (-1_800_000_000, 1_800_000_000)
    assert (head["min_lat_e7"], head["max_lat_e7"]) == (-850_511_287, 850_511_287)
    assert (head["center_lon_e7"], head["center_lat_e7"]) == (0, 0)
    assert reader.metadata()["vector_layers"][0]["id"] == ds.slug
    assert reader.get(0, 0, 0) is None
    assert list(all_tiles(MemorySource(paths[0].read_bytes()))) == []


def test_a_polygon_layer_whose_shapes_make_no_tile_makes_an_empty_archive(tmp_path: Path) -> None:
    # A line in a polygon layer: the tiles keep only polygons, so no tile is made.
    row = _connect().execute("SELECT ST_AsWKB(ST_GeomFromText('LINESTRING(150 -30, 151 -29)'))")
    ds = make_dataset(
        [Field("code", "code")],
        title="T",
        geometry={"kind": "polygon", "crs": "EPSG:7844", "maxzoom": 4},
    )
    tbl = Table(
        dataset=ds,
        manifest=make_manifest(b""),
        table=pa.table({"code": ["1"]}),
        geometry=pa.array([bytes(present(row.fetchone())[0])], pa.binary()),
    )
    path = tmp_path / "data.pmtiles"
    write_pmtiles(tbl, make_header(1, "data.pmtiles"), path)
    assert Reader(MemorySource(path.read_bytes())).header()["tile_entries_count"] == 0
