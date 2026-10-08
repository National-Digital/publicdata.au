import filecmp
import gzip
import io
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pyarrow.feather as pf
import pyarrow.parquet as pq

from publicdata import serialise
from publicdata.__main__ import main
from publicdata.build import build_dataset
from publicdata.database import build_database
from publicdata.dbcheck import compare
from publicdata.gate import check
from publicdata.register import load, parse
from publicdata.serialise import (
    CAPS,
    EXCEL_MAX_ROWS,
    cappable,
    duckdb_digest,
    formats_for,
    legacy_left_out,
    over_cap,
)
from publicdata.store import Manifest

from .conftest import make_manifest, read_json

if TYPE_CHECKING:
    import pytest


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_fixture_build_is_deterministic_and_carries_provenance(
    register_dir: Path, fixture_store: Path, tmp_path: Path
) -> None:
    ds = {d.slug: d for d in load(register_dir)}["qld-road-crash-factors"]
    outs = []
    for name in ("a", "b"):
        out = tmp_path / name
        out.mkdir()
        built = build_dataset(ds, fixture_store, out)
        outs.append(out)
    a, b = outs
    assert _tree(a) == _tree(b)
    # A DuckDB file is compared by content, since its bytes are not reproducible.
    files = [f for f in _tree(a) if not f.endswith("data.duckdb")]
    _same, diff, err = filecmp.cmpfiles(a, b, files, shallow=False)
    assert not diff
    assert not err
    assert compare(a, b) == []
    vdir = a / "d" / ds.slug / "v" / "2026-04-24"
    data = read_json(vdir / "data.json")
    h = data["publicdata"]
    assert h["licence"]["id"] == "CC-BY-4.0"
    assert "sourced" in h["attribution"]
    assert h["source"]["sha256"]
    assert h["not_endorsed"]
    assert len(data["records"]) == data["publicdata"]["rows"] == 300
    assert data["records"][0]["involving_drink_driving"] in (True, False)
    meta = pq.read_metadata(vdir / "data.parquet").metadata
    assert meta is not None
    assert json.loads(meta[b"publicdata"])["dataset"] == ds.slug
    con = sqlite3.connect(vdir / "data.sqlite")
    assert con.execute("select count(*) from records").fetchone()[0] == 300
    assert (
        con.execute("select value from publicdata where key='version'").fetchone()[0]
        == "2026-04-24"
    )
    # The publisher's file is served from the raw store, so the tree lists it and holds none.
    assert not (vdir / "source.csv").exists()
    latest = built.latest
    assert latest is not None
    assert latest.files["source.csv"] == latest.manifest.bytes > 0
    versions = read_json(a / "d" / ds.slug / "versions.json")
    assert versions["latest"] == "2026-04-24"
    assert versions["versions"][0]["rows"] == 300
    idx = read_json(vdir / "by" / "crash_year" / "index.json")
    assert sum(p["rows"] for p in idx["partitions"]) == 300
    assert (a / "d" / ds.slug / "datapackage.json").exists()
    assert (a / "d" / ds.slug / "history.tar.zst").exists()
    shutil.rmtree(a)


def test_geometry_fixture_writes_valid_excel_and_geopackage_and_no_arrow(
    register_dir: Path, fixture_store: Path, tmp_path: Path
) -> None:
    ds = {d.slug: d for d in load(register_dir)}["qld-road-crash-locations"]
    outs = []
    for name in ("a", "b"):
        out = tmp_path / name
        out.mkdir()
        build_dataset(ds, fixture_store, out)
        outs.append(out)
    a, b = outs
    _same, diff, err = filecmp.cmpfiles(a, b, _tree(a), shallow=False)
    assert not diff
    assert not err
    vdir = a / "d" / ds.slug / "v" / "2026-04-24"
    for f in (
        "data.xlsx",
        "data.gpkg",
        "data.geo.parquet",
        "data.csv.gz",
        "schema.sql",
        "data.csv-metadata.json",
    ):
        assert (vdir / f).exists(), f
    # Fetched after the caps came in: no Arrow, and the sizes that picked the formats are recorded.
    assert not (vdir / "data.arrow").exists()
    man = read_json(vdir / "manifest.json")
    assert man["measured_bytes"] == {
        f: (vdir / f).stat().st_size for f in ("data.ndjson", "data.csv", "data.geojson")
    }
    assert man["formats_left_out"] == {}
    with zipfile.ZipFile(vdir / "data.xlsx") as z:
        names = z.namelist()
        assert "xl/worksheets/sheet1.xml" in names
        assert "xl/worksheets/sheet3.xml" in names
        assert all(i.date_time == (2026, 4, 24, 0, 0, 0) for i in z.infolist())
    con = sqlite3.connect(vdir / "data.gpkg")
    assert con.execute("PRAGMA application_id").fetchone()[0] == 1196444487
    assert con.execute("select table_name, data_type, srs_id from gpkg_contents").fetchone() == (
        "records",
        "features",
        7844,
    )
    n, with_geom = con.execute("select count(*), count(geom) from records").fetchone()
    assert n == 300
    assert 0 < with_geom <= n
    blob = con.execute("select geom from records where geom is not null limit 1").fetchone()[0]
    assert blob[:2] == b"GP"
    assert len(blob) == 61
    con.close()
    with gzip.open(vdir / "data.csv.gz", "rb") as gz:
        assert gz.read() == (vdir / "data.csv").read_bytes()
    sql = (vdir / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE qld_road_crash_locations (" in sql
    assert 'PRIMARY KEY ("crash_ref_number")' in sql
    csvw = read_json(vdir / "data.csv-metadata.json")
    assert csvw["url"] == "data.csv"
    assert csvw["tableSchema"]["primaryKey"] == ["crash_ref_number"]
    dp = read_json(a / "d" / ds.slug / "datapackage.json")
    resources = {r["name"] for r in dp["resources"]}
    assert resources >= {"xlsx", "gpkg", "geo.parquet", "csv.gz", "schema-sql", "csvw"}
    assert "arrow" not in resources


def test_a_version_fetched_before_the_caps_keeps_its_arrow_file(
    register_dir: Path, fixture_store: Path, tmp_path: Path
) -> None:
    ds = {d.slug: d for d in load(register_dir)}["qld-road-casualties"]
    build_dataset(ds, fixture_store, tmp_path)
    vdir = tmp_path / "d" / ds.slug / "v" / "2026-04-24"
    t = pf.read_table(vdir / "data.arrow")
    assert t.num_rows == 300
    assert json.loads(t.schema.metadata[b"publicdata"])["dataset"] == ds.slug
    man = read_json(vdir / "manifest.json")
    assert "measured_bytes" not in man
    assert "formats_left_out" not in man
    dp = read_json(tmp_path / "d" / ds.slug / "datapackage.json")
    assert "arrow" in {r["name"] for r in dp["resources"]}


def test_excel_is_skipped_above_the_row_limit(
    register_dir: Path, fixture_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serialise, "EXCEL_MAX_ROWS", 100)
    assert "xlsx" not in formats_for(300, geometry=False)
    assert "xlsx" in formats_for(100, geometry=False)
    ds = {d.slug: d for d in load(register_dir)}["qld-road-crash-factors"]
    build_dataset(ds, fixture_store, tmp_path)
    vdir = tmp_path / "d" / ds.slug / "v" / "2026-04-24"
    assert not (vdir / "data.xlsx").exists()
    assert (vdir / "data.csv.gz").exists()
    dp = read_json(tmp_path / "d" / ds.slug / "datapackage.json")
    assert "xlsx" not in {r["name"] for r in dp["resources"]}
    assert not [e for e in check(tmp_path, register_dir) if "xlsx" in e]


def test_json_and_geojson_skip_the_row_limit_before_the_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serialise, "JSON_MAX_ROWS", 100)
    assert {"json", "geojson"} & set(formats_for(300, geometry=True)) == set()
    assert {"json", "geojson", "gpkg", "arrow"} <= set(formats_for(100, geometry=True))
    assert set(legacy_left_out(300, geometry=True)) == {"json", "geojson"}
    assert set(legacy_left_out(300, geometry=False)) == {"json"}


def test_the_caps_decide_by_the_measured_sizes_and_a_record_fixes_the_set() -> None:
    assert cappable(geometry=False) == ["sqlite", "xlsx", "json"]
    assert cappable(geometry=True) == ["sqlite", "geojson", "xlsx", "json"]
    assert cappable("polygon") == ["sqlite", "geojson", "xlsx", "json"]
    for f, (_on, limit) in CAPS.items():
        assert over_cap(f, 10, limit) is None
        assert over_cap(f, 10, limit + 1)
    # Just over a limit reads in bytes, so the size never looks equal to the limit.
    assert (over_cap("xlsx", 10, CAPS["xlsx"][1] + 1) or "").startswith(
        "Excel is not offered because the table is 50,000,001 bytes as CSV, over the 50 MB limit"
    )
    assert (over_cap("xlsx", 10, 60_000_000) or "").startswith(
        "Excel is not offered because the table is 60.0 MB as CSV, over the 50 MB limit"
    )
    assert over_cap("geojson", 10, 7_800_000_000) == (
        "GeoJSON is not offered because the file would be 7,800.0 MB, over the 100 MB limit "
        "for GeoJSON."
    )
    assert over_cap("xlsx", EXCEL_MAX_ROWS + 1, 1) == (
        "Excel is not offered because the table is over 1,048,575 rows, which is as "
        "many as a worksheet holds below its header row."
    )
    assert formats_for(10, geometry=True, gone={}) == [
        "json", "ndjson", "csv", "parquet", "sqlite", "duckdb", "xlsx", "csv.gz",
        "geojson", "gpkg", "geo.parquet",
    ]  # fmt: skip
    # The recorded set is the set, whatever the rows: no rule is applied again.
    assert "xlsx" in formats_for(EXCEL_MAX_ROWS + 1, geometry=False, gone={})
    gone = {"json": "x", "sqlite": "y"}
    assert {"json", "sqlite", "arrow"} & set(formats_for(10, "polygon", gone)) == set()
    assert {"ndjson", "csv", "csv.gz", "parquet", "duckdb", "pmtiles"} <= set(
        formats_for(10, "polygon", gone)
    )


def test_a_layers_geojson_is_measured_on_its_own_bytes(
    register_dir: Path, fixture_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = {d.slug: d for d in load(register_dir)}["abs-lga-2025"]
    build_dataset(ds, fixture_store, tmp_path)
    vdir = tmp_path / "d" / ds.slug / "v" / "2026-05-14"
    man = read_json(vdir / "manifest.json")
    gj = (vdir / "data.geojson").stat().st_size
    nd = (vdir / "data.ndjson").stat().st_size
    assert man["measured_bytes"]["data.geojson"] == gj
    assert gj > 10 * nd
    assert man["formats_left_out"] == {}
    assert not (vdir / "data.arrow").exists()
    # Under a cap the NDJSON would pass, the file itself is over, so it is dropped.
    monkeypatch.setitem(serialise.CAPS, "geojson", ("geojson", nd + 1))
    out = tmp_path / "capped"
    build_dataset(ds, fixture_store, out)
    vdir = out / "d" / ds.slug / "v" / "2026-05-14"
    man = read_json(vdir / "manifest.json")
    assert not (vdir / "data.geojson").exists()
    assert (vdir / "data.pmtiles").exists()
    assert set(man["formats_left_out"]) == {"geojson"}
    assert man["measured_bytes"]["data.geojson"] == gj


def test_a_published_versions_format_set_never_moves(
    register_dir: Path, fixture_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = {d.slug: d for d in load(register_dir)}["qld-road-crash-locations"]
    vdir = tmp_path / "d" / ds.slug / "v" / "2026-04-24"
    build_dataset(ds, fixture_store, tmp_path)
    first = read_json(vdir / "manifest.json")
    assert first["formats_left_out"] == {}
    assert first["caps"] == 1
    # A tighter cap later leaves a rebuilt published version as it was.
    monkeypatch.setitem(serialise.CAPS, "json", ("ndjson", 1_000))
    out = build_dataset(ds, fixture_store, tmp_path)
    assert (vdir / "data.json").exists()
    assert out.latest is not None
    assert out.latest.left_out == {}
    assert read_json(vdir / "manifest.json") == first
    # A new version, with nothing published, takes the cap.
    fresh = tmp_path / "fresh"
    out = build_dataset(ds, fixture_store, fresh)
    assert out.latest is not None
    assert set(out.latest.left_out or ()) == {"json"}
    assert not (fresh / "d" / ds.slug / "v" / "2026-04-24" / "data.json").exists()
    # The gate holds the record to the files beside it.
    man = read_json(vdir / "manifest.json")
    man["measured_bytes"]["data.csv"] += 1
    (vdir / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    assert any(
        "measured_bytes disagrees with data.csv" in e
        for e in check(tmp_path, register_dir, site=False)
    )


def test_formats_over_their_caps_are_left_out_and_the_pages_say_why(
    register_dir: Path, fixture_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(serialise.CAPS, "json", ("ndjson", 1_000))
    monkeypatch.setitem(serialise.CAPS, "geojson", ("geojson", 1_000))
    out = tmp_path / "dist"
    assert main(["build", "--store", str(fixture_store), "--out", str(out)]) == 0
    slug = "qld-road-crash-locations"
    vdir = out / "d" / slug / "v" / "2026-04-24"
    rows = read_json(vdir / "manifest.json")["rows"]
    assert rows > 100
    assert not (vdir / "data.json").exists()
    assert not (vdir / "data.geojson").exists()
    assert (vdir / "data.ndjson").exists()
    assert (vdir / "data.gpkg").exists()
    assert list((vdir / "by").glob("*/*.geojson"))  # partition files are slices and stay
    gone = read_json(vdir / "manifest.json")["formats_left_out"]
    assert set(gone) == {"json", "geojson"}
    assert gone["json"].startswith("JSON is not offered because the table is ")
    vpage = (vdir / "index.html").read_text(encoding="utf-8")
    assert gone["json"] in vpage
    assert gone["geojson"] in vpage
    assert gone["json"] in (vdir / "index.md").read_text(encoding="utf-8")
    dp = read_json(out / "d" / slug / "datapackage.json")
    assert {"json", "geojson"} & {r["name"] for r in dp["resources"]} == set()
    page = (out / "d" / slug / "index.html").read_text(encoding="utf-8")
    assert "latest/data.ndjson" in page
    assert "v/2026-04-24/data.json" not in page
    title = page.split("<title>", 1)[1].split("</title>", 1)[0]
    assert "JSON" not in title
    assert "CSV, Parquet, SQLite" in title
    assert "data.json" not in next(
        ln for ln in (out / "llms.txt").read_text().splitlines() if slug in ln
    )
    assert gone["geojson"] in page
    assert check(out, register_dir) == []
    # The gate refuses a file past its cap, on disk or claimed as already published.
    rel = vdir.relative_to(out).as_posix()
    assert any("data.json is published though the caps leave it out" in e for e in check(out, register_dir, [f"{rel}/data.json"]))  # fmt: skip
    (vdir / "data.geojson").write_text("{}", encoding="utf-8")
    assert any(
        "data.geojson is published though the caps leave it out" in e
        for e in check(out, register_dir)
    )
    (vdir / "data.geojson").unlink()
    (vdir / "data.arrow").write_bytes(b"")
    assert any(
        "data.arrow is published though the caps leave it out" in e
        for e in check(out, register_dir)
    )
    (vdir / "data.arrow").unlink()
    (vdir / "index.html").write_text(vpage.replace(gone["json"], ""), encoding="utf-8")
    assert any("does not say why data.json" in e for e in check(out, register_dir))


def test_a_database_fixture_builds_one_duckdb_and_a_parquet_per_table(  # noqa: PLR0915 - one fixture build, checked file by file
    register_dir: Path, fixture_store: Path, tmp_path: Path
) -> None:
    ds = {d.slug: d for d in load(register_dir)}["gnaf"]
    outs = []
    for name in ("a", "b"):
        out = tmp_path / name
        out.mkdir()
        outs.append((out, build_dataset(ds, fixture_store, out)))
    (a, oa), (b, _) = outs
    files = [f for f in _tree(a) if not f.endswith("data.duckdb")]
    _same, diff, err = filecmp.cmpfiles(a, b, files, shallow=False)
    assert not diff
    assert not err
    # The DuckDB files are compared by content, since their bytes are not reproducible.
    assert compare(a, b) == []
    vdir = a / "d" / ds.slug / "v" / "2026-08-17"
    v = oa.latest
    assert v is not None
    assert v.tables["address_detail"] == 40
    assert len(v.tables) == 37
    assert v.rows == sum(v.tables.values())
    assert not (vdir / "data.csv").exists()
    assert not (vdir / "data.parquet").exists()
    for t in ds.tables:
        assert (vdir / "tables" / f"{t.name}.parquet").exists()
    meta = pq.read_metadata(vdir / "tables" / "address_detail.parquet")
    assert meta.num_rows == 40
    assert meta.metadata is not None
    assert json.loads(meta.metadata[b"publicdata"])["licence"]["condition"].startswith(
        "You must not"
    )
    con = duckdb.connect()
    con.execute(f"ATTACH '{vdir / 'data.duckdb'}' AS g (READ_ONLY)")
    assert con.execute("SELECT count(*) FROM g.address_view").fetchall()[0][0] == 38
    assert con.execute("SELECT count(*) FROM g.state").fetchall()[0][0] == 1
    typed = dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'address_detail'"
        ).fetchall()
    )
    assert typed["date_created"] == "DATE"
    assert typed["confidence"] == "BIGINT"
    assert con.execute("SELECT count(*) FROM g.relations").fetchall()[0][0] == 47
    assert (
        json.loads(
            con.execute("SELECT value FROM g.publicdata WHERE key = 'licence'").fetchall()[0][0]
        )["id"]
        == "OPEN-GNAF-EULA"
    )
    con.close()
    man = read_json(vdir / "manifest.json")
    assert man["kind"] == "database"
    assert man["unknown_upstream_columns"] == []
    assert man["unknown_upstream_tables"] == []
    schema = read_json(vdir / "schema.json")
    detail = next(t for t in schema["tables"] if t["name"] == "address_detail")
    assert detail["primaryKey"] == ["address_detail_pid"]
    assert {
        "fields": ["locality_pid"],
        "reference": {"resource": "locality", "fields": ["locality_pid"]},
    } in detail["foreignKeys"]
    sql = (vdir / "schema.sql").read_text(encoding="utf-8")
    assert 'FOREIGN KEY ("locality_pid") REFERENCES "locality" ("locality_pid")' in sql
    assert 'CREATE VIEW "address_view" AS' in sql
    # The archive stays in the store, and the version lists it at its size there.
    assert not (vdir / "source.zip").exists()
    assert v.files["source.zip"] == v.manifest.bytes
    pkg = read_json(a / "d" / ds.slug / "datapackage.json")
    assert pkg["publicdata:kind"] == "database"
    assert pkg["resources"][0]["name"] == "duckdb"
    assert pkg["licenses"][0]["publicdata:condition"]
    versions = read_json(a / "d" / ds.slug / "versions.json")
    assert versions["versions"][0]["tables"]["locality"] == 19


def test_a_table_version_carries_a_duckdb_file_with_typed_columns_and_provenance(
    register_dir: Path, fixture_store: Path, tmp_path: Path
) -> None:
    ds = {d.slug: d for d in load(register_dir)}["qld-road-crash-factors"]
    build_dataset(ds, fixture_store, tmp_path)
    path = tmp_path / "d" / ds.slug / "v" / "2026-04-24" / "data.duckdb"
    con = duckdb.connect()
    con.execute(f"ATTACH '{path}' AS q (READ_ONLY)")
    assert con.execute("SELECT count(*) FROM q.records").fetchall()[0][0] == 300
    types = dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'records'"
        ).fetchall()
    )
    assert types["crash_year"] == "BIGINT"
    assert types["involving_drink_driving"] == "BOOLEAN"
    assert (
        con.execute("SELECT value FROM q.publicdata WHERE key = 'version'").fetchall()[0][0]
        == "2026-04-24"
    )
    assert con.execute("SELECT count(*) FROM q.fields").fetchall()[0][0] == len(ds.fields)
    con.close()
    # A small table is written with 16 KB blocks, so the file is small.
    assert path.stat().st_size < 1_000_000
    assert duckdb_digest(path) == duckdb_digest(path)


def test_a_database_reads_tab_separated_members_and_keeps_default_blocks(tmp_path: Path) -> None:
    raw = {
        "slug": "tabbed",
        "kind": "database",
        "title": "Tabbed",
        "status": "building",
        "publisher": {"name": "Agency", "jurisdiction": "Cth"},
        "licence": {
            "id": "CC-BY-4.0",
            "evidence": "https://example.gov.au/",
            "attribution": "A.",
            "reviewed": "2026-10-01",
        },
        "source": {
            "adapter": "ckan-resource",
            "url": "https://example.gov.au/",
            "package": "x",
            "resource_match": "zip$",
        },
        "database": {"member_match": r"(?P<table>[A-Z]+)\.tsv$", "delimiter": "tab"},
        "tables": [
            {
                "name": "thing",
                "source": "THING",
                "key": ["id"],
                "fields": [
                    {"name": "id", "source": "ID", "type": "integer"},
                    {"name": "name", "source": "NAME"},
                    {"name": "seen", "source": "SEEN", "type": "date"},
                ],
            }
        ],
    }
    ds = parse(raw, "tabbed")
    assert ds.database is not None
    assert ds.database.delimiter == "\t"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/THING.tsv", "ID\tNAME\tSEEN\n1\tone\t2026-01-02\n2\t\t\n")
    data = buf.getvalue()
    src = tmp_path / "source.zip"
    src.write_bytes(data)
    m = make_manifest(data, dataset="tabbed", filename="source.zip", encoding="zip")
    vdir = tmp_path / "v"
    out = build_database(
        ds,
        m,
        src,
        vdir,
        lambda rows, rel: {"version": m.version, "url": f"https://x/{rel}", "attribution": "A."},
    )
    assert out.tables == {"thing": 2}
    assert out.unknown_columns == {}
    assert out.unknown_tables == []
    con = duckdb.connect()
    con.execute(f"ATTACH '{vdir / 'data.duckdb'}' AS t (READ_ONLY)")
    assert con.execute("SELECT id, name, seen FROM t.thing ORDER BY id").fetchall() == [
        (1, "one", __import__("datetime").date(2026, 1, 2)),
        (2, None, None),
    ]
    # A database keeps DuckDB's default block size, so a large file reads in few range requests.
    assert (
        con.execute(
            "SELECT block_size FROM pragma_database_size() WHERE database_name = 't'"
        ).fetchall()[0][0]
        == 262144
    )
    con.close()


def test_a_manifest_with_a_field_this_code_does_not_know_still_reads(tmp_path: Path) -> None:
    src = Path(__file__).parent / "fixtures" / "store" / "qld-road-crash-factors" / "2026-04-24"
    d = read_json(src / "manifest.json")
    (tmp_path / "manifest.json").write_text(json.dumps({**d, "later": 2}), encoding="utf-8")
    m = Manifest.read(tmp_path / "manifest.json")
    assert m.caps == 1
    assert not hasattr(m, "later")


def test_a_limited_build_measures_what_it_does_not_keep(
    register_dir: Path, fixture_store: Path, tmp_path: Path
) -> None:
    ds = {d.slug: d for d in load(register_dir)}["qld-road-crash-locations"]
    serialise.LIMIT = {"ndjson", "parquet"}
    try:
        build_dataset(ds, fixture_store, tmp_path)
    finally:
        serialise.LIMIT = None
    vdir = tmp_path / "d" / ds.slug / "v" / "2026-04-24"
    assert not (vdir / "data.csv").exists()
    assert not (vdir / "data.geojson").exists()
    man = read_json(vdir / "manifest.json")
    assert set(man["measured_bytes"]) == {"data.ndjson", "data.csv", "data.geojson"}
    full = tmp_path / "full"
    build_dataset(ds, fixture_store, full)
    assert read_json(full / "d" / ds.slug / "v" / "2026-04-24" / "manifest.json") == man
