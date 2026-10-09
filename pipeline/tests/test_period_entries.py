"""The eucalypt records and the daily dam levels, split by year from their next fetch: their
register entries, the parts the dates the providers publish land in, the versions already
published, and the pages and hubs that read a version written as parts alone."""

import datetime as dt
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from publicdata import gate, parts, periods, store
from publicdata.build import build_dataset, version_key
from publicdata.cache import BuildCache
from publicdata.register import load

from .conftest import make_manifest

REPO = Path(__file__).parents[2]
EUC, WATER = "au-eucalypt-records", "au-water-storage-levels"
EUC_COLS = (
    "uuid,occurrenceID,raw_catalogNumber,raw_institutionCode,raw_collectionCode,scientificName,"
    "vernacularName,taxonRank,species,eventDate,year,month,stateProvince,decimalLatitude,"
    "decimalLongitude,coordinateUncertaintyInMeters,basisOfRecord,dataResourceUid,"
    "dataResourceName,license,typeStatus,locality,occurrenceStatus,firstLoadedDate"
)


def entries():
    return {d.slug: d for d in load(REPO / "register") if d.slug in (EUC, WATER)}


def ms(day: str) -> int:
    t = dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC)
    return int(t.timestamp() * 1000)


def euc_row(n: int, day: str | None, year: str, lat: float = -33.9, lon: float = 151.2) -> str:
    when = str(ms(day)) if day else ""
    return (
        f"u{n},o{n},C{n},MEL,MEL,Eucalyptus robusta,Swamp Mahogany,species,Eucalyptus robusta,"
        f"{when},{year},,New South Wales,{lat},{lon},1000,PRESERVED_SPECIMEN,dr376,"
        f"National Herbarium of Victoria (MEL),CC-BY 4.0,,Botany Bay,PRESENT,2014-05-01T00:00:00Z"
    )


# The dates as the providers publish them: 1599 as a placeholder year, a row with no date or
# year, the oldest plausible record from 1770, and a row with a year but no event date.
EUC_ROWS = [
    euc_row(1, "1599-12-31", "1599"),
    euc_row(2, None, ""),
    euc_row(3, "1770-04-28", "1770"),
    euc_row(4, "2019-05-01", "2019", -37.8, 144.9),
    euc_row(5, "2019-11-30", "2019", -31.9, 115.8),
    euc_row(6, None, "1950", -42.9, 147.3),
]


def put(st: Path, ds, version: str, body: str, period=True) -> store.Manifest:
    data = body.encode()
    m = make_manifest(
        data,
        dataset=ds.slug,
        version=version,
        as_at=None,
        fetched_at=f"{version}T01:00:00+00:00",
        filename=f"{ds.slug}.csv",
        source={"url": ds.source.url},
        licence={"id": ds.licence.id, "read_from": ds.licence.evidence, "read_at": version},
    )
    if period:
        m.period = periods.recorded(ds.period)
    store.write(st, m, data)
    return m


def spine_store(tmp: Path) -> Path:
    """A store holding the fixture place spine, which the eucalypt entry joins its points to."""
    st = tmp / "store"
    for layer in (Path(__file__).parent / "fixtures" / "store").glob("abs-*"):
        shutil.copytree(layer, st / layer.name)
    return st


def built_manifest(out: Path, slug: str, version: str) -> dict:
    return json.loads((out / "d" / slug / "v" / version / "manifest.json").read_text("utf-8"))


def test_both_entries_split_by_year_and_neither_waits_on_the_pending_list():
    got = entries()
    assert got[EUC].period == periods.Period("event_date", "year")
    assert got[WATER].period == periods.Period("date", "year")
    assert EUC not in gate.PERIOD_PENDING and WATER not in gate.PERIOD_PENDING


def test_the_versions_already_published_keep_their_cache_keys(tmp_path):
    """A period is recorded by the fetch, and these versions were fetched without one, so the
    register change keys them as before and the build reuses what it published."""
    cache = BuildCache(tmp_path / "cache")
    for slug, ds in entries().items():
        ms_ = store.manifests(REPO / "store", slug)
        assert ms_ and all(m.period is None for m in ms_)
        bare = replace(ds, period=None)
        for m in ms_:
            assert version_key(cache, ds, m, REPO / "store") == version_key(
                cache, bare, m, REPO / "store"
            )


def test_a_version_fetched_before_the_period_builds_byte_identical(tmp_path):
    from publicdata.spine import SOURCE_PREFIX

    st = tmp_path / "store"
    ds = entries()[EUC]
    # Without the place joins, which need the spine and have no bearing on the period.
    own = tuple(f for f in ds.fields if not f.source.startswith(SOURCE_PREFIX))
    ds = replace(ds, enrich=(), fields=own)
    put(st, ds, "2026-09-24", "\n".join([EUC_COLS, *EUC_ROWS]) + "\n", period=False)
    a, b = tmp_path / "a", tmp_path / "b"
    build_dataset(replace(ds, period=None), st, a)
    build_dataset(ds, st, b)
    files = sorted(p.relative_to(a) for p in (a / "d").rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(b) for p in (b / "d").rglob("*") if p.is_file())
    assert not any("parts" in p.parts for p in files)
    for rel in files:
        if rel.name != "data.duckdb":
            assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


def test_eucalypt_dates_land_in_their_year_and_rows_with_no_date_stay_undated(euc):
    out = euc["whole"]
    m = built_manifest(out, EUC, "2026-10-13")
    by = {p["period"]: p for p in m["parts"]}
    assert list(by) == ["1599", "1770", "2019", "undated"]
    assert sum(p["rows"] for p in m["parts"]) == m["rows"] == len(EUC_ROWS)
    vdir = out / "d" / EUC / "v" / "2026-10-13"
    ids = {k: pq.read_table(vdir / p["files"]["parquet"]["path"]).column("record_id").to_pylist() for k, p in by.items()}  # fmt: skip
    assert ids == {"1599": ["u1"], "1770": ["u3"], "2019": ["u4", "u5"], "undated": ["u2", "u6"]}
    whole = pq.read_table(vdir / "data.parquet")
    undated = pq.read_table(vdir / "parts/undated.parquet")
    assert undated.column("year").to_pylist() == [None, 1950]
    assert sorted(whole.column("record_id").to_pylist()) == sorted(sum(ids.values(), []))


def water_rows(extra: list[str]) -> str:
    rows = [
        "212243.1,Warragamba,NSW,1905-01-01,1000.5,10",
        "212243.1,Warragamba,NSW,2024-06-01,1800000.0,10",
        "212243.1,Warragamba,NSW,2025-06-01,1850000.0,10",
        "143036A,Wivenhoe,QLD,2026-10-01,1100000.0,90",
        *extra,
    ]
    return "\n".join(["station_no,station_name,state,date,value,quality_code", *rows]) + "\n"


def test_a_finished_dam_levels_year_is_reused_by_the_next_fetch(tmp_path):
    st = tmp_path / "store"
    ds = entries()[WATER]
    put(st, ds, "2026-10-01", water_rows([]))
    put(st, ds, "2026-10-08", water_rows(["143036A,Wivenhoe,QLD,2026-10-07,1101000.0,90"]))
    out = tmp_path / "dist"
    o = build_dataset(ds, st, out)
    first, second = (built_manifest(out, WATER, v) for v in ("2026-10-01", "2026-10-08"))
    trees = {p["period"]: p["tree"] for p in second["parts"]}
    assert trees == {"1905": "2026-10-01", "2024": "2026-10-01", "2025": "2026-10-08", "2026": "2026-10-08"}  # fmt: skip
    assert not (out / "d" / WATER / "v" / "2026-10-08" / "parts" / "1905.parquet").exists()
    assert [p["rows"] for p in second["parts"]] == [1, 1, 1, 2]
    assert sum(p["rows"] for p in first["parts"]) == first["rows"] == 4
    assert not any(p["revised"] for p in second["parts"])
    assert o.latest.manifest.version == "2026-10-08"
    from publicdata.site import render_site

    render_site([o], out)
    page = (out / "d" / WATER / "index.html").read_text("utf-8")
    assert "1905 (unchanged since 1 October 2026)" in page
    assert "2025 (unchanged since" not in page and "2026 (unchanged since" not in page


def test_the_gate_passes_both_entries_over_the_threshold(tmp_path):
    site = tmp_path / "site"
    (site / "d").mkdir(parents=True)
    latest, cat = {}, []
    for slug in (EUC, WATER):
        vdir = site / "d" / slug / "v" / "2026-10-01"
        vdir.mkdir(parents=True)
        (vdir / "manifest.json").write_text(json.dumps({"rows": 6_500_000}))
        latest[slug] = "2026-10-01"
        cat.append({"identifier": slug, "distribution": [{"format": "parquet", "byteSize": 200 * 1024 * 1024}]})  # fmt: skip
    (site / "latest.json").write_text(json.dumps(latest))
    (site / "catalog.json").write_text(json.dumps({"dataset": cat}))
    reg = entries()
    assert gate.periods_needed(site, reg) == []
    bare = {k: replace(v, period=None) for k, v in reg.items()}
    assert len(gate.periods_needed(site, bare)) == 2


@pytest.fixture(scope="module")
def euc(tmp_path_factory):
    """The eucalypt fixture fetched with its period, built and rendered whole, and again as a
    version too large to be one file."""
    from publicdata.site import render_site

    tmp = tmp_path_factory.mktemp("eucalypts")
    st = spine_store(tmp)
    ds = entries()[EUC]
    put(st, ds, "2026-10-13", "\n".join([EUC_COLS, *EUC_ROWS]) + "\n")
    whole = tmp / "whole"
    render_site([build_dataset(ds, st, whole)], whole)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(parts, "WHOLE_BYTES", 0)
        split = tmp / "split"
        o = build_dataset(ds, st, split)
    render_site([o], split)
    return {"whole": whole, "split": split, "out": o}


def hero_total(out: Path) -> int:
    page = (out / "index.html").read_text("utf-8")
    return int(re.search(r"([\d,]+) eucalypt records from the herbaria", page)[1].replace(",", ""))


def test_the_home_page_map_draws_every_record_from_the_parts(euc):
    whole, split, o = euc["whole"], euc["split"], euc["out"]
    assert o.latest.whole is False
    assert not (split / "d" / EUC / "v" / "2026-10-13" / "data.parquet").exists()
    assert hero_total(split) == hero_total(whole) == o.latest.rows == len(EUC_ROWS)


def test_a_parts_only_page_still_draws_its_chart_map_and_sample(euc):
    whole, split = euc["whole"], euc["split"]
    a = (whole / "d" / EUC / "index.html").read_text("utf-8")
    b = (split / "d" / EUC / "index.html").read_text("utf-8")
    for marker in ('class="fchart"', "counted in cells of", 'class="ledger sample"'):
        assert marker in a and marker in b, marker
    assert "data-no-query" in b
    assert 'id="parts"' in b and "1599" in b and "undated" in b
    sample = re.compile(r'<table class="ledger sample">.*?</table>', re.S)
    assert sample.search(b)[0].count("<tr>") == sample.search(a)[0].count("<tr>") > 1


def test_a_cached_build_pulls_a_parts_only_version_s_parts_back_for_its_pages(euc, tmp_path):
    """A part a cached build left out is read back through published, as data.parquet is."""
    from publicdata import published
    from publicdata.site import render_site

    out = Path(shutil.copytree(euc["split"], tmp_path / "out"))
    gone = list((out / "d" / EUC / "v" / "2026-10-13" / "parts").glob("*.parquet"))
    assert gone
    for p in gone:
        p.unlink()
    published.current = published.Published(out, [], euc["split"])
    try:
        render_site([euc["out"]], out)
    finally:
        published.current = None
    assert hero_total(out) == len(EUC_ROWS)
    assert all(p.exists() for p in gone)


def test_the_gate_checks_the_grain_of_a_split_version_and_passes_it(euc):
    out = euc["split"]
    assert built_manifest(out, EUC, "2026-10-13")["period"]["grain"] == "year"
    assert gate.periods_needed(out, entries()) == []


def test_a_parts_only_dam_levels_version_keeps_the_explorer_and_names_the_hubs_version(tmp_path):
    from publicdata.site import render_site

    st = tmp_path / "store"
    ds = entries()[WATER]
    put(st, ds, "2026-10-01", water_rows([]))
    put(st, ds, "2026-10-08", water_rows(["143036A,Wivenhoe,QLD,2026-10-07,1101000.0,90"]))
    out = tmp_path / "dist"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(parts, "WHOLE_ROWS", 4)
        o = build_dataset(ds, st, out)
    assert [v.whole for v in o.versions] == [True, False]
    render_site([o], out, hubs={"datasets": {WATER: {"zenodo": "10.5281/zenodo.1"}}})
    explore = (out / "d" / WATER / "explore" / "index.html").read_text("utf-8")
    assert "version 2026-10-01, the newest published as one file" in explore
    assert "(latest)" not in explore
    assert (out / "d" / WATER / "embed" / "index.html").exists()
    page = (out / "d" / WATER / "index.html").read_text("utf-8")
    assert '<span class="mono">2026-10-01</span>, the newest published as one file' in page


def test_no_explorer_opens_an_older_version_when_the_newest_whole_one_is_too_large(tmp_path):
    from publicdata import explorer
    from publicdata.site import render_site

    st = tmp_path / "store"
    ds = entries()[WATER]
    put(st, ds, "2026-10-01", water_rows([]))
    put(st, ds, "2026-10-08", water_rows(["143036A,Wivenhoe,QLD,2026-10-07,1101000.0,90"]))
    put(
        st,
        ds,
        "2026-10-15",
        water_rows(
            [
                "143036A,Wivenhoe,QLD,2026-10-07,1101000.0,90",
                "143036A,Wivenhoe,QLD,2026-10-14,1102000.0,90",
            ]
        ),
    )
    out = tmp_path / "dist"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(parts, "WHOLE_ROWS", 5)
        o = build_dataset(ds, st, out)
    assert [v.whole for v in o.versions] == [True, True, False]
    # The newest whole version is past what the explorer loads, so only the oldest would fit.
    o.versions[1].files["data.parquet"] = explorer.MAX_PARQUET + 1
    render_site([o], out)
    assert not (out / "d" / WATER / "explore" / "index.html").exists()
    assert not (out / "d" / WATER / "embed" / "index.html").exists()
    page = (out / "d" / WATER / "index.html").read_text("utf-8")
    assert "/explore/" not in page
