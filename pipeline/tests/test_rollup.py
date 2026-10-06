import datetime as dt
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from publicdata import cache, rollup

FIX = Path(__file__).parent / "fixtures" / "rollup"
ARROW = {"string": pa.string(), "integer": pa.int64(), "boolean": pa.bool_(), "date": pa.date32()}


def _ds(fields, **kw):
    base = dict(
        slug="t",
        kind="table",
        query=True,
        example=None,
        chart=None,
        fields=tuple(SimpleNamespace(name=f["name"], type=f["type"]) for f in fields),
    )
    return SimpleNamespace(**(base | kw))


def _cell(f, v):
    if v is None:
        return None
    if f["type"] == "boolean":
        return bool(v)
    if f["type"] == "date":
        return dt.date.fromisoformat(v)
    return v


def _parquet(path: Path, fields, rows, header=None) -> Path:
    cols = {f["name"]: [_cell(f, r[i]) for r in rows] for i, f in enumerate(fields)}
    schema = pa.schema([(f["name"], ARROW[f["type"]]) for f in fields])
    t = pa.table(cols, schema=schema)
    t = t.replace_schema_metadata({"publicdata": json.dumps(header or {"attribution": "A"})})
    pq.write_table(t, path)
    return path


@pytest.fixture
def fixture(tmp_path):
    d = json.loads((FIX / "rows.json").read_text(encoding="utf-8"))
    return d, _parquet(tmp_path / "data.parquet", d["fields"], d["rows"])


def test_the_committed_rollup_is_what_build_writes(fixture):
    # functions/_rollup.test.mjs reads this file, so the two languages share one format.
    d, path = fixture
    run, _ = rollup.parquet_run(path)
    p = rollup.Plan(tuple(tuple(c) for c in d["cubes"]), tuple(d["metrics"]), ())
    body = rollup.build(_ds(d["fields"]), run, p, rollup.parquet_header(path), "t", "2026-01-01")
    assert body == (FIX / "rollup.json.gz").read_bytes()
    obj = json.loads(gzip.decompress(body))
    assert obj["rows"] == len(d["rows"])
    assert obj["publicdata"] == {"attribution": "A"}
    for c in obj["cubes"]:
        assert sum(c["count"]) == len(d["rows"])
        # A field never totals itself, and values are listed in SQLite's order.
        assert not set(c["dims"]) & set(c["metrics"])
        assert all(v == sorted(v, key=rollup._order) for v in c["values"])
    assert obj["cubes"][0]["values"][0][0] is None


def _wide(n=20_000):
    fields = [
        {"name": "id", "type": "integer"},
        {"name": "kind", "type": "string"},
        {"name": "region", "type": "string"},
        {"name": "year", "type": "integer"},
        {"name": "n", "type": "integer"},
    ]
    rows = [(i, f"k{i % 3}", f"r{i % 97}", 2000 + i % 25, i % 5) for i in range(n)]
    return fields, rows


def test_plan_answers_the_register_first_and_leaves_out_row_ids(tmp_path):
    fields, rows = _wide()
    run, _ = rollup.parquet_run(_parquet(tmp_path / "a.parquet", fields, rows))
    example = {
        "group": ("kind",),
        "filters": ({"field": "region", "op": "eq", "value": "r1"},),
        "metric": "sum.n",
    }
    p = rollup.plan(_ds(fields, example=example), run, len(rows))
    assert ("kind", "region") in p.cubes
    assert not any("id" in c for c in p.cubes)
    assert p.metrics[0] == "n"
    # Every count by one field has a cube to come from, alone or in a pair.
    assert all(any(f in c for c in p.cubes) for f in ("kind", "region", "year", "n"))


def test_plan_keeps_within_the_cap_and_skips_small_tables(tmp_path):
    fields, rows = _wide()
    run, _ = rollup.parquet_run(_parquet(tmp_path / "b.parquet", fields, rows))
    p = rollup.plan(_ds(fields), run, len(rows), cap=6_000)
    assert (
        sum(
            rollup.estimate(g, len(c), len(p.metrics))
            for c, g in zip(p.cubes, p.groups, strict=True)
        )
        <= 6_000
    )
    # A plan whose estimate fits but whose bytes do not loses its last cubes until it fits.
    p, body = rollup.make(_ds(fields), run, len(rows), {}, "t", "v", cap=3_000)
    assert 0 < len(body) <= 3_000 and p.cubes
    small = _parquet(tmp_path / "c.parquet", fields, rows[:100])
    assert rollup.plan(_ds(fields), rollup.parquet_run(small)[0], 100) is None


def test_write_skips_what_r2_holds_and_tables_out_of_the_api(tmp_path):
    fields, rows = _wide()
    for v in ("2026-01-01", "2026-02-01"):
        dest = tmp_path / "dist" / "d" / "t" / "v" / v / "data.parquet"
        dest.parent.mkdir(parents=True)
        _parquet(dest, fields, rows)
    out = tmp_path / "out"
    have = {rollup.key("t", "2026-01-01")}
    got = rollup.write([tmp_path / "dist"], [_ds(fields)], have, out, log=lambda *_: None)
    assert [p.relative_to(out).as_posix() for p in got] == ["_rollup/t/2026-02-01.json.gz"]
    assert rollup.write([tmp_path / "dist"], [_ds(fields, query=False)], set(), out) == []


def test_rollups_do_not_key_the_build_cache():
    assert Path(rollup.__file__).resolve() not in cache.code_files()
