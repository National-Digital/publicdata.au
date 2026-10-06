import json
import random
import sqlite3

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from publicdata import fetch, store
from publicdata.build import build_dataset, build_version
from publicdata.cache import BuildCache
from publicdata.register import Field, RegisterError, parse
from publicdata.serialise import profile

from .conftest import make_dataset, make_manifest
from .test_cache import _reading
from .test_register import _raw

F = [
    Field("id", "Id", "integer"),
    Field("year", "Year", "integer"),
    Field("place", "Place"),
    Field("big", "Big", "integer"),
]
# Ids run backwards and places repeat, so the sort, the key and the source position each decide
# some pair of rows.
CSV = (
    b"Id,Year,Place,Big\n"
    b"6,2024,Bay,5000000000\n"
    b"5,2023,Hill,1\n"
    b"4,2024,Bay,2\n"
    b"3,,Hill,3\n"
    b"2,2023,Bay,4\n"
    b"1,2023,Hill,\n"
)


def _build(tmp_path, csv=CSV, **kw):
    ds = make_dataset(F, key=("id",), **kw)
    m = make_manifest(csv)
    _, vout = build_version(ds, m, csv, tmp_path)
    return ds, tmp_path / "d" / "t" / "v" / m.version


def _kv(path):
    return pq.read_metadata(path).metadata


def test_a_sorted_parquet_follows_the_sort_then_the_key_and_names_its_profile(tmp_path):
    _, vdir = _build(tmp_path, sort=("year", "place"))
    t = pq.read_table(vdir / "data.parquet")
    # Nulls last, then the key breaks the ties the sort leaves.
    assert t.column("id").to_pylist() == [2, 1, 5, 4, 6, 3]
    meta = pq.read_metadata(vdir / "data.parquet")
    assert meta.metadata[profile.KEY.encode()] == profile.VERSION.encode()
    assert json.loads(meta.metadata[b"publicdata"])["dataset"] == "t"
    names = [meta.schema.column(c.column_index).name for c in meta.row_group(0).sorting_columns]
    assert names == ["year", "place", "id"]
    assert not any(c.nulls_first or c.descending for c in meta.row_group(0).sorting_columns)
    col = meta.row_group(0).column(0)
    assert col.has_offset_index and col.has_column_index and col.compression == "ZSTD"


def test_ties_after_the_key_keep_the_source_position(tmp_path):
    ds = make_dataset(F, sort=("place",))
    t = pa.table({"id": [1, 2, 3, 4], "place": ["b", "a", "b", "a"]})
    assert profile.ordered(t, ds.sort, ds.key).column("id").to_pylist() == [2, 4, 1, 3]


def test_the_other_formats_keep_the_source_order_and_duckdb_follows_the_parquet(tmp_path):
    _, vdir = _build(tmp_path, sort=("year", "place"))
    source = [6, 5, 4, 3, 2, 1]
    lines = (vdir / "data.csv").read_text().splitlines()[1:]
    assert [int(r.split(",")[0]) for r in lines] == source
    nd = (vdir / "data.ndjson").read_text().splitlines()[1:]
    assert [json.loads(r)["id"] for r in nd] == source
    assert [r["id"] for r in json.loads((vdir / "data.json").read_text())["records"]] == source
    con = sqlite3.connect(vdir / "data.sqlite")
    assert [r[0] for r in con.execute("SELECT id FROM records ORDER BY rowid")] == source
    db = duckdb.connect(str(vdir / "data.duckdb"), read_only=True)
    assert [r[0] for r in db.execute("SELECT id FROM records").fetchall()] == [2, 1, 5, 4, 6, 3]


def test_without_a_sort_the_rows_keep_the_source_order_and_no_page_index(tmp_path):
    _, vdir = _build(tmp_path)
    assert pq.read_table(vdir / "data.parquet").column("id").to_pylist() == [6, 5, 4, 3, 2, 1]
    meta = pq.read_metadata(vdir / "data.parquet")
    assert meta.metadata[profile.KEY.encode()] == profile.VERSION.encode()
    assert not meta.row_group(0).sorting_columns
    assert not meta.row_group(0).column(0).has_column_index


def test_integers_that_fit_32_bits_are_int32_and_read_back_as_int64(tmp_path):
    _, vdir = _build(tmp_path)
    schema = pq.read_schema(vdir / "data.parquet")
    assert schema.field("id").type == pa.int32() and schema.field("year").type == pa.int32()
    assert schema.field("big").type == pa.int64()
    back = profile.widen(pq.read_table(vdir / "data.parquet"))
    assert back.schema.field("id").type == pa.int64()
    # An all-null integer column fits.
    t = pa.table({"n": pa.nulls(3, pa.int64())})
    assert profile.int32_columns(t) == ["n"]


def test_lookup_fields_get_bloom_filters(tmp_path):
    _, vdir = _build(tmp_path, lookup=("place",))
    rows = duckdb.sql(
        f"SELECT path_in_schema, bloom_filter_offset IS NOT NULL FROM parquet_metadata('{vdir / 'data.parquet'}')"
    ).fetchall()
    assert {p for p, has in rows if has} == {"place"}


def test_two_writes_of_one_table_are_byte_identical(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        _build(tmp_path / name, sort=("place",), lookup=("id",))
    rel = "d/t/v/2026-01-02/data.parquet"
    assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()


def test_a_re_sort_cuts_no_new_version():
    lines = CSV.splitlines(keepends=True)
    rows = lines[1:]
    random.Random(7).shuffle(rows)
    shuffled = lines[0] + b"".join(rows)
    plain = make_dataset(F, key=("id",))
    sorted_ = make_dataset(F, key=("id",), sort=("year", "place"), lookup=("place",))
    digest = fetch.rows_digest(plain, make_manifest(CSV), CSV)
    assert digest
    assert fetch.rows_digest(plain, make_manifest(shuffled), shuffled) == digest
    assert fetch.rows_digest(sorted_, make_manifest(CSV), CSV) == digest


def test_a_cached_sorted_version_grows_formats_in_the_source_order(tmp_path, monkeypatch):
    from publicdata import build, serialise

    s = tmp_path / "store"
    store.write(s, make_manifest(CSV), CSV)
    ds = make_dataset(F, key=("id",), sort=("year", "place"))
    plain = tmp_path / "plain"
    build_dataset(ds, s, plain)
    cache = BuildCache(tmp_path / "cache")
    monkeypatch.setattr(serialise, "LIMIT", {"ndjson", "csv", "parquet"})
    few = tmp_path / "few"
    build_dataset(ds, s, few, cache)
    monkeypatch.setattr(serialise, "LIMIT", None)
    monkeypatch.setattr(
        build, "normalise", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rebuilt"))
    )
    grown = tmp_path / "grown"
    _reading(monkeypatch, grown, few)
    build_dataset(ds, s, grown, BuildCache(tmp_path / "cache"))
    rel = "d/t/v/2026-01-02"
    for f in ("data.json", "data.sqlite", "data.xlsx", "data.arrow"):
        assert (grown / rel / f).read_bytes() == (plain / rel / f).read_bytes(), f


def test_a_sorted_version_without_its_order_is_built_again(tmp_path, monkeypatch):
    from publicdata import build, serialise

    s = tmp_path / "store"
    store.write(s, make_manifest(CSV), CSV)
    ds = make_dataset(F, key=("id",), sort=("place",))
    cache = BuildCache(tmp_path / "cache")
    monkeypatch.setattr(serialise, "LIMIT", {"ndjson", "csv", "parquet"})
    build_dataset(ds, s, tmp_path / "few", cache)
    monkeypatch.setattr(serialise, "LIMIT", None)
    _reading(monkeypatch, tmp_path / "grown", tmp_path / "few")
    for p in cache.root.glob(f"*/{build.ORDER}"):
        p.unlink()
    rebuilt = []
    real = build.build_version
    monkeypatch.setattr(build, "build_version", lambda *a, **k: rebuilt.append(1) or real(*a, **k))
    build_dataset(ds, s, tmp_path / "grown", BuildCache(tmp_path / "cache"))
    assert rebuilt


@pytest.mark.parametrize(
    "over,match",
    [
        ({"sort": ["nope"]}, "sort field 'nope' is not a declared field"),
        ({"sort": ["a", "a"]}, "sort names a field twice"),
        ({"sort": "a"}, "sort is a list of fields"),
        ({"lookup": ["flag"]}, "lookup field 'flag' is a boolean"),
    ],
)
def test_sort_and_lookup_name_declared_fields(over, match):
    fields = [
        {"name": "a", "source": "A"},
        {"name": "flag", "source": "Flag", "type": "boolean"},
    ]
    with pytest.raises(RegisterError, match=match):
        parse(_raw(fields=fields, **over), "x")
    ds = parse(_raw(fields=fields, sort=["a"], lookup=["a"]), "x")
    assert ds.sort == ("a",) and ds.lookup == ("a",)
