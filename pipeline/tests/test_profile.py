import json
import random
import sqlite3
from dataclasses import replace
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from publicdata import fetch, store
from publicdata.build import build_dataset, build_version
from publicdata.cache import BuildCache
from publicdata.register import Field, RegisterError, Source, load, parse
from publicdata.serialise import profile

from .conftest import ROOT, make_dataset, make_manifest
from .test_cache import _reading
from .test_register import _raw

F = [
    Field("id", "Id", "integer"),
    Field("year", "Year", "integer"),
    Field("place", "Place"),
    Field("big", "Big", "integer"),
]
# Ids run backwards and places repeat, so the sort, the key and the source position each decide
# some pair of rows, and one year is blank.
CSV = (
    b"Id,Year,Place,Big\n"
    b"6,2024,Bay,5000000000\n"
    b"5,2023,Hill,1\n"
    b"4,2024,Bay,2\n"
    b"3,,Hill,3\n"
    b"2,2023,Bay,4\n"
    b"1,2023,Hill,\n"
)
SOURCE = [6, 5, 4, 3, 2, 1]
SORTED = [2, 1, 5, 4, 6, 3]


def _ds(**kw):
    return make_dataset(F, key=("id",), **kw)


def _m(ds, csv=CSV, legacy=False, **kw):
    return make_manifest(csv, parquet={} if legacy else profile.layout(ds), **kw)


def _build(tmp_path, ds, legacy=False):
    m = _m(ds, legacy=legacy)
    _, vout = build_version(ds, m, CSV, tmp_path)
    return tmp_path / "d" / "t" / "v" / m.version, vout


def _ids(path):
    return pq.read_table(path).column("id").to_pylist()


def test_a_sorted_parquet_follows_the_sort_then_the_key_and_names_its_profile(tmp_path):
    vdir, _ = _build(tmp_path, _ds(sort=("year", "place")))
    # Nulls last, then the key breaks the ties the sort leaves.
    assert _ids(vdir / "data.parquet") == SORTED
    meta = pq.read_metadata(vdir / "data.parquet")
    assert meta.metadata[profile.KEY.encode()] == profile.VERSION.encode()
    assert json.loads(meta.metadata[b"publicdata"])["dataset"] == "t"
    names = [meta.schema.column(c.column_index).name for c in meta.row_group(0).sorting_columns]
    assert names == ["year", "place", "id"]
    assert not any(c.nulls_first or c.descending for c in meta.row_group(0).sorting_columns)
    col = meta.row_group(0).column(0)
    assert col.has_offset_index and col.has_column_index and col.compression == "ZSTD"
    assert profile.signature(vdir / "data.parquet") == "year,place,id"


def test_ties_after_the_key_keep_the_source_position():
    t = pa.table({"id": [1, 2, 3, 4], "place": ["b", "a", "b", "a"]})
    assert profile.ordered(t, ("place",), ()).column("id").to_pylist() == [2, 4, 1, 3]


def test_the_other_formats_keep_the_source_order_and_duckdb_follows_the_parquet(tmp_path):
    vdir, _ = _build(tmp_path, _ds(sort=("year", "place")))
    lines = (vdir / "data.csv").read_text().splitlines()[1:]
    assert [int(r.split(",")[0]) for r in lines] == SOURCE
    nd = (vdir / "data.ndjson").read_text().splitlines()[1:]
    assert [json.loads(r)["id"] for r in nd] == SOURCE
    assert [r["id"] for r in json.loads((vdir / "data.json").read_text())["records"]] == SOURCE
    con = sqlite3.connect(vdir / "data.sqlite")
    assert [r[0] for r in con.execute("SELECT id FROM records ORDER BY rowid")] == SOURCE
    db = duckdb.connect(str(vdir / "data.duckdb"), read_only=True)
    assert [r[0] for r in db.execute("SELECT id FROM records").fetchall()] == SORTED


@pytest.mark.parametrize("legacy", [False, True])
def test_the_duckdb_file_is_written_in_one_insert(tmp_path, monkeypatch, legacy):
    # DuckDB writes a larger file when the rows arrive in several inserts, so the slices a sort
    # reads in are streamed into one.
    from publicdata.serialise.writers import duckdb as writer

    real_chunks, real_connect = profile.chunks, writer.duckdb_connect
    monkeypatch.setattr(profile, "chunks", lambda t, perm, rows=2: real_chunks(t, perm, rows))
    inserts = []

    class Counting:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, *a):
            inserts.extend([sql] if sql.startswith("INSERT INTO records") else [])
            return self.con.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self.con, name)

    monkeypatch.setattr(writer, "duckdb_connect", lambda *a: Counting(real_connect(*a)))
    vdir, _ = _build(tmp_path, _ds(sort=("year", "place")), legacy=legacy)
    assert len(inserts) == 1
    db = duckdb.connect(str(vdir / "data.duckdb"), read_only=True)
    ids = [r[0] for r in db.execute("SELECT id FROM records").fetchall()]
    assert ids == (SOURCE if legacy else SORTED)


def test_the_build_sorts_a_version_once(tmp_path, monkeypatch):
    calls = []
    real = profile.permutation
    monkeypatch.setattr(profile, "permutation", lambda *a: calls.append(1) or real(*a))
    from publicdata import build

    monkeypatch.setattr(build, "permutation", profile.permutation)
    _build(tmp_path, _ds(sort=("year", "place")))
    assert len(calls) == 1


def test_without_a_sort_the_rows_keep_the_source_order_and_no_page_index(tmp_path):
    vdir, _ = _build(tmp_path, _ds())
    assert _ids(vdir / "data.parquet") == SOURCE
    meta = pq.read_metadata(vdir / "data.parquet")
    assert meta.metadata[profile.KEY.encode()] == profile.VERSION.encode()
    assert not meta.row_group(0).sorting_columns
    assert not meta.row_group(0).column(0).has_column_index
    assert profile.signature(vdir / "data.parquet") == ""


def test_a_version_fetched_before_the_profile_keeps_its_writer_and_gets_a_query_copy(tmp_path):
    ds = _ds(sort=("year", "place"), int32=("id",))
    vdir, vout = _build(tmp_path, ds, legacy=True)
    meta = pq.read_metadata(vdir / "data.parquet")
    assert profile.KEY.encode() not in meta.metadata
    assert pq.read_schema(vdir / "data.parquet").field("id").type == pa.int64()
    assert _ids(vdir / "data.parquet") == SOURCE
    db = duckdb.connect(str(vdir / "data.duckdb"), read_only=True)
    assert [r[0] for r in db.execute("SELECT id FROM records").fetchall()] == SOURCE
    q = tmp_path / vout.query
    assert vout.query == "_q/t/2026-01-02.parquet"
    assert _ids(q) == SORTED and profile.signature(q) == "year,place,id"
    assert pq.read_schema(q).field("id").type == pa.int32()
    # The query copy carries the version's own provenance, which names its data.parquet.
    assert pq.read_metadata(q).metadata[b"publicdata"] == meta.metadata[b"publicdata"]


def test_a_profile_versions_query_copy_is_its_data_parquet(tmp_path):
    vdir, vout = _build(tmp_path, _ds(sort=("place",)))
    q = tmp_path / vout.query
    assert q.read_bytes() == (vdir / "data.parquet").read_bytes()


def test_a_version_keeps_the_layout_it_was_fetched_with(tmp_path):
    old = _ds(sort=("place",))
    m = _m(old)
    now = _ds(sort=("year", "place"))
    _, vout = build_version(now, m, CSV, tmp_path)
    vdir = tmp_path / "d" / "t" / "v" / m.version
    assert _ids(vdir / "data.parquet") == [2, 4, 6, 1, 3, 5]
    assert _ids(tmp_path / vout.query) == SORTED


def test_int32_is_declared_per_field(tmp_path):
    vdir, _ = _build(tmp_path, _ds(int32=("id", "year")))
    schema = pq.read_schema(vdir / "data.parquet")
    assert schema.field("id").type == pa.int32() and schema.field("year").type == pa.int32()
    assert schema.field("big").type == pa.int64()
    back = profile.widen(pq.read_table(vdir / "data.parquet"))
    assert back.schema.field("id").type == pa.int64()
    # A field declared INT32 stays INT32 in a version that holds no value, so every version of
    # it has one type, and an undeclared one is never narrowed by its values.
    t = pa.table({"n": pa.nulls(3, pa.int64()), "m": pa.array([1, 2, 3], pa.int64())})
    narrowed = profile.narrow(t, ["n"])
    assert narrowed.schema.field("n").type == pa.int32()
    assert narrowed.schema.field("m").type == pa.int64()


def test_a_version_that_overflows_an_int32_field_stops_the_build(tmp_path):
    with pytest.raises(ValueError, match="big holds 1 to 5000000000, outside 32 bits"):
        _build(tmp_path, _ds(int32=("big",)))


def test_lookup_fields_get_bloom_filters(tmp_path):
    vdir, _ = _build(tmp_path, _ds(lookup=("place",)))
    rows = duckdb.sql(
        f"SELECT path_in_schema, bloom_filter_offset IS NOT NULL FROM parquet_metadata('{vdir / 'data.parquet'}')"
    ).fetchall()
    assert {p for p, has in rows if has} == {"place"}


def test_two_builds_of_a_sorted_version_with_a_blank_sort_value_are_byte_identical(tmp_path):
    ds = _ds(sort=("year", "place"), lookup=("id",), int32=("id",))
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        _build(tmp_path / name, ds)
    for rel in ("d/t/v/2026-01-02/data.parquet", "_q/t/2026-01-02.parquet"):
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()


def test_two_builds_of_a_sorted_layer_are_byte_identical(tmp_path):
    reg = {d.slug: d for d in load(ROOT / "register")}
    ds = replace(reg["abs-lga-2025"], sort=("state_name",))
    fixtures = ROOT / "pipeline" / "tests" / "fixtures" / "store"
    m = store.manifests(fixtures, ds.slug)[-1]
    m = replace(m, parquet=profile.layout(ds))
    data = store.source_path(fixtures, m).read_bytes()
    outs = []
    for name in ("a", "b"):
        _, vout = build_version(ds, m, data, tmp_path / name, fixtures)
        outs.append(tmp_path / name / "d" / ds.slug / "v" / m.version / "data.parquet")
    a, b = outs
    assert a.read_bytes() == b.read_bytes()
    names = pq.read_table(a).column("state_name").to_pylist()
    assert names == sorted(names)
    assert b"geo" in pq.read_metadata(a).metadata


def test_a_re_sort_cuts_no_new_version():
    lines = CSV.splitlines(keepends=True)
    rows = lines[1:]
    random.Random(7).shuffle(rows)
    shuffled = lines[0] + b"".join(rows)
    plain = _ds()
    sorted_ = _ds(sort=("year", "place"), lookup=("place",), int32=("id",))
    digest = fetch.rows_digest(plain, make_manifest(CSV), CSV)
    assert digest
    assert fetch.rows_digest(plain, make_manifest(shuffled), shuffled) == digest
    assert fetch.rows_digest(sorted_, make_manifest(CSV), CSV) == digest


def test_a_fetch_records_the_layout_its_version_keeps(tmp_path, monkeypatch):
    ds = _ds(sort=("place",), source=Source(adapter="file", url="https://e/f.csv"))
    lic = {"id": "CC-BY-4.0", "read_from": "https://e", "read_at": "2026-10-01T00:00:00+00:00"}
    m = make_manifest(CSV, dataset="t", version="2026-10-01")
    monkeypatch.setitem(fetch.ADAPTERS, "file", lambda d, s: (CSV, m, lic))
    got = fetch.fetch(ds, tmp_path)
    assert got.parquet == profile.layout(ds)
    assert store.manifests(tmp_path, "t")[0].parquet["sort"] == ["place"]


def _few_then_grown(tmp_path, monkeypatch, ds, stale_published=None):
    from publicdata import build, serialise

    s = tmp_path / "store"
    store.write(s, _m(ds), CSV)
    cache = BuildCache(tmp_path / "cache")
    monkeypatch.setattr(serialise, "LIMIT", {"ndjson", "csv", "parquet"})
    few = tmp_path / "few"
    build_dataset(ds, s, few, cache)
    monkeypatch.setattr(serialise, "LIMIT", None)
    if stale_published is not None:
        stale_published(few / "d" / "t" / "v" / "2026-01-02" / "data.parquet")
    plain = tmp_path / "plain"
    build_dataset(ds, s, plain)
    rebuilt = []
    real = build.build_version
    monkeypatch.setattr(build, "build_version", lambda *a, **k: rebuilt.append(1) or real(*a, **k))
    grown = tmp_path / "grown"
    _reading(monkeypatch, grown, few)
    build_dataset(ds, s, grown, BuildCache(tmp_path / "cache"))
    return grown, plain, rebuilt


def test_a_cached_sorted_version_grows_formats_in_the_source_order(tmp_path, monkeypatch):
    grown, plain, rebuilt = _few_then_grown(tmp_path, monkeypatch, _ds(sort=("year", "place")))
    assert not rebuilt
    rel = "d/t/v/2026-01-02"
    for f in ("data.json", "data.sqlite", "data.xlsx", "data.arrow", "data.duckdb"):
        assert (grown / rel / f).exists(), f
    for f in ("data.json", "data.sqlite", "data.xlsx", "data.arrow"):
        assert (grown / rel / f).read_bytes() == (plain / rel / f).read_bytes(), f


def test_a_sorted_version_without_its_order_is_built_again(tmp_path, monkeypatch):
    from publicdata import build

    ds = _ds(sort=("place",))

    def drop(_):
        for p in (tmp_path / "cache").glob(f"*/{build.ORDER}"):
            p.unlink()

    _, _, rebuilt = _few_then_grown(tmp_path, monkeypatch, ds, drop)
    assert rebuilt


def test_a_published_parquet_from_an_older_build_is_never_reordered(tmp_path, monkeypatch):
    """The published tree holds an older, unsorted, unprofiled Parquet of the version: the order
    the cache recorded belongs to another file, so the version is built again from its source
    and its other formats keep the publisher's order."""
    ds = _ds(sort=("year", "place"))

    def older(p):
        t = pq.read_table(p)
        p.unlink()
        pq.write_table(
            profile.widen(profile.apply(t, pa.array(pc_sort_back(t)))).replace_schema_metadata(
                {"publicdata": t.schema.metadata[b"publicdata"]}
            ),
            p,
            row_group_size=65_536,
        )

    grown, plain, rebuilt = _few_then_grown(tmp_path, monkeypatch, ds, older)
    assert rebuilt
    rel = "d/t/v/2026-01-02"
    nd = (grown / rel / "data.ndjson").read_text().splitlines()[1:]
    assert [json.loads(r)["id"] for r in nd] == SOURCE
    for f in ("data.json", "data.sqlite", "data.xlsx", "data.arrow", "data.parquet"):
        assert (grown / rel / f).read_bytes() == (plain / rel / f).read_bytes(), f


def pc_sort_back(t):
    """Positions that put a sorted table's rows back in id order, descending, as published."""
    ids = t.column("id").to_pylist()
    return sorted(range(len(ids)), key=lambda i: -ids[i])


def test_d1_loads_a_version_again_when_its_rows_were_taken_in_another_order(fixture_site, tmp_path):
    from publicdata import d1

    slug, version = "qld-road-crash-locations", "2026-04-24"
    ds = {d.slug: d for d in load(ROOT / "register")}[slug]
    src = fixture_site / "d" / slug / "v" / version / "data.parquet"
    fields = {(slug, version): json.dumps(d1.built_fields(ds, src))}
    loaded = {slug: [version]}
    stale = d1.write_loads([fixture_site], [ds], loaded, tmp_path / "a", "", fields, {})
    assert stale  # loaded before its rows were sorted
    same = {(slug, version): profile.signature(src)}
    assert not d1.write_loads([fixture_site], [ds], loaded, tmp_path / "b", "", fields, same)
    text = "".join(p.read_text() for p in stale)
    assert "INSERT OR REPLACE INTO _orders VALUES ('qld-road-crash-locations'" in text


def test_the_gate_wants_every_table_version_to_have_its_query_copy(site_copy):
    from publicdata.gate import check

    q = "_q/qld-road-crash-factors/2026-04-24.parquet"
    assert (site_copy / q).is_file()
    (site_copy / q).unlink()
    errors = check(site_copy, ROOT / "register")
    assert f"qld-road-crash-factors/2026-04-24: missing its query copy {q}" in errors
    assert check(site_copy, ROOT / "register", [q]) == []


def test_query_copies_go_to_r2_alone(tmp_path):
    from publicdata.__main__ import main
    from publicdata.r2 import dated_file

    out, large = tmp_path / "dist", tmp_path / "large"
    (out / "_q" / "x").mkdir(parents=True)
    (out / "_q" / "x" / "2026-04-24.parquet").write_text("x")
    assert main(["split", "--out", str(out), "--large", str(large)]) == 0
    assert (large / "_q" / "x" / "2026-04-24.parquet").is_file()
    assert not (out / "_q" / "x" / "2026-04-24.parquet").exists()
    assert dated_file("_q/x/2026-04-24.parquet")


@pytest.mark.parametrize(
    "over,match",
    [
        ({"sort": ["nope"]}, "sort field 'nope' is not a declared field"),
        ({"sort": ["a", "a"]}, "sort names a field twice"),
        ({"sort": "a"}, "sort is a list of fields"),
        ({"lookup": ["flag"]}, "lookup field 'flag' is a boolean"),
        ({"int32": ["a"]}, "int32 field 'a' is not an integer"),
    ],
)
def test_sort_lookup_and_int32_name_declared_fields(over, match):
    fields = [
        {"name": "a", "source": "A"},
        {"name": "n", "source": "N", "type": "integer"},
        {"name": "flag", "source": "Flag", "type": "boolean"},
    ]
    with pytest.raises(RegisterError, match=match):
        parse(_raw(fields=fields, **over), "x")
    ds = parse(_raw(fields=fields, sort=["a"], lookup=["a"], int32=["n"]), "x")
    assert (ds.sort, ds.lookup, ds.int32) == (("a",), ("a",), ("n",))


def test_a_fetch_holds_a_version_that_does_not_fit_an_int32_field(tmp_path, monkeypatch):
    ds = _ds(int32=("big",), source=Source(adapter="file", url="https://e/f.csv"))
    lic = {"id": "CC-BY-4.0", "read_from": "https://e", "read_at": "2026-10-01T00:00:00+00:00"}
    m = make_manifest(CSV, dataset="t", version="2026-10-01")
    monkeypatch.setitem(fetch.ADAPTERS, "file", lambda d, s: (CSV, m, lic))
    with pytest.raises(fetch.FetchError, match="t: big holds 1 to 5000000000, outside 32 bits"):
        fetch.fetch(ds, tmp_path)
    assert store.manifests(tmp_path, "t") == []


def test_register_validate_checks_int32_against_the_versions_at_hand(tmp_path):
    from publicdata.validate import int32_misfits

    s = tmp_path / "store"
    plain = _ds()
    m = _m(plain)
    store.write(s, m, CSV)
    declared = _ds(int32=("id", "big"))
    assert int32_misfits(declared, m, s, []) == ["big holds 1 to 5000000000, outside 32 bits"]
    assert int32_misfits(_ds(int32=("id",)), m, s, []) == []
    # A built Parquet answers from its statistics, before the source.
    build_version(plain, m, CSV, tmp_path / "dist")
    (s / "t" / m.version / "source.csv").unlink()
    assert int32_misfits(declared, m, s, [tmp_path / "dist"]) == [
        "big holds 1 to 5000000000, outside 32 bits"
    ]
    assert int32_misfits(declared, m, s, []) is None


def test_validate_reads_every_column_when_a_file_has_no_statistics(tmp_path):
    from publicdata.validate import _parquet_misfits

    path = tmp_path / "x.parquet"
    pq.write_table(pa.table({"a": [1, 2], "b": [1, 2**40]}), path, write_statistics=False)
    assert _parquet_misfits(path, ("a", "b")) == _parquet_misfits(path, ("b",)) != []


def test_validate_reports_a_source_that_no_longer_normalises(tmp_path, monkeypatch):
    from publicdata import normalise
    from publicdata.validate import int32_misfits

    s = tmp_path / "store"
    m = _m(_ds())
    store.write(s, m, CSV)

    def broken(*a, **k):
        raise ValueError("bad row")

    monkeypatch.setattr(normalise, "normalise", broken)
    assert int32_misfits(_ds(int32=("id",)), m, s, []) == [
        "its source no longer normalises (bad row)"
    ]


def test_the_order_file_is_in_the_entry_before_its_record(tmp_path):
    cache = BuildCache(tmp_path / "cache")
    seen = []
    real = Path.write_text

    def spy(self, *a, **k):
        if self.name == "meta.json":
            seen.append((self.parent / "order.parquet").is_file())
        return real(self, *a, **k)

    import unittest.mock

    with unittest.mock.patch.object(Path, "write_text", spy):
        cache.put("k", {}, extra={"order.parquet": b"x"})
    assert seen == [True]


def test_sort_or_int32_on_a_database_names_the_rule():
    for name in ("sort", "int32"):
        with pytest.raises(RegisterError, match=f"{name} is for a table entry"):
            parse(_raw(kind="database", **{name: ["a"]}), "x")


@pytest.mark.parametrize("legacy", [False, True])
def test_the_sample_note_names_the_order_the_rows_are_in(tmp_path, legacy):
    from publicdata.site import _sample

    ds = _ds(sort=("year", "place"))
    vdir, _ = _build(tmp_path, ds, legacy=legacy)
    note = json.dumps(_sample(ds, vdir / "data.parquet"))
    if legacy:
        assert "in the publisher's order" in note and "sorted by" not in note
    else:
        assert "sorted by year, then place" in note and "publisher's order" not in note


def test_a_footer_shows_the_layout_it_was_written_with(tmp_path):
    ds = _ds(sort=("year", "place"), lookup=("place",), int32=("id",))
    vdir, vout = _build(tmp_path, ds, legacy=True)
    lay = profile.layout(ds)
    assert profile.follows(pq.read_metadata(tmp_path / vout.query), lay)
    assert not profile.follows(pq.read_metadata(vdir / "data.parquet"), lay)
    for edit in ({"sort": ["place"]}, {"lookup": []}, {"int32": ["id", "year"]}, {"key": []}):
        assert not profile.follows(pq.read_metadata(tmp_path / vout.query), lay | edit)
    assert profile.layout_key(vout.query) == "_q/t/2026-01-02.layout.json"
