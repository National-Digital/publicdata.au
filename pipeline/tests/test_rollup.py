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
ARROW = {
    "string": pa.string(),
    "integer": pa.int64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "number": pa.float64(),
}


def _ds(fields, **kw):
    base = dict(
        slug="t",
        kind="table",
        query=True,
        example=None,
        chart=None,
        rollup=(),
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


def test_plan_holds_the_register_rollup_sets(tmp_path):
    fields, rows = _wide()
    run, _ = rollup.parquet_run(_parquet(tmp_path / "a.parquet", fields, rows))
    declared = ("kind", "region", "year")
    p = rollup.plan(_ds(fields, rollup=(declared,)), run, len(rows))
    assert tuple(sorted(declared)) in p.cubes
    # Left to itself the plan holds pairs at most.
    assert all(len(c) <= 2 for c in rollup.plan(_ds(fields), run, len(rows)).cubes)


def test_a_cube_is_not_charged_for_totals_of_the_fields_it_groups_on():
    assert rollup.cube_estimate(100, ("a", "m"), ("m", "x")) == rollup.estimate(100, 2, 1)
    assert rollup.cube_estimate(100, ("a",), ("m", "x")) == rollup.estimate(100, 1, 2)


def test_served_lists_every_published_version_of_an_entry_out_of_d1(tmp_path):
    fields, _ = _wide()
    root = tmp_path / "dist"
    (root / "d" / "t").mkdir(parents=True)
    listed = [
        {"version": "2026-01-01", "rows": 7_000, "tombstone": None},
        {"version": "2026-02-01", "rows": 8_000},
        {"version": "2025-01-01", "rows": 9_000, "tombstone": {"reason": "takedown"}},
    ]
    (root / "d" / "t" / "versions.json").write_text(json.dumps({"versions": listed}))
    logs = []
    off = [_ds(fields, query=False), _ds(fields, slug="u", query=False)]
    got = rollup.served([*off, _ds(fields, slug="q")], [tmp_path / "none", root], log=logs.append)
    assert got == {"t": {"2026-01-01": 7_000, "2026-02-01": 8_000}}
    assert logs == ["rollup: u has no versions.json in the built tree, so it gets no rollups"]


def test_plan_keeps_within_the_cap_and_skips_small_tables(tmp_path):
    fields, rows = _wide()
    run, _ = rollup.parquet_run(_parquet(tmp_path / "b.parquet", fields, rows))
    p = rollup.plan(_ds(fields), run, len(rows), cap=6_000)
    assert (
        sum(rollup.cube_estimate(g, c, p.metrics) for c, g in zip(p.cubes, p.groups, strict=True))
        <= 6_000
    )
    # A plan whose estimate fits but whose bytes do not loses its last cubes until it fits.
    p, body = rollup.make(_ds(fields), run, len(rows), {}, "t", "v", cap=3_000)
    assert 0 < len(body) <= 3_000 and p.cubes
    small = _parquet(tmp_path / "c.parquet", fields, rows[:100])
    assert rollup.plan(_ds(fields), rollup.parquet_run(small)[0], 100) is None


class FakeStore:
    """R2 as write() sees it: published Parquet by version, and each rollup's stamp."""

    def __init__(self, parquets: dict[tuple[str, str], Path], stamps=None):
        self.parquets, self.stamps, self.fetched = parquets, dict(stamps or {}), []

    def parquet(self, slug, version):
        p = self.parquets.get((slug, version))
        return rollup.file_identity(p) if p else None

    def stamp(self, k):
        return self.stamps.get(k)

    def fetch(self, slug, version, dest):
        self.fetched.append((slug, version))
        dest.write_bytes(self.parquets[(slug, version)].read_bytes())


def _versions(tmp_path, fields, rows, versions=("2026-01-01", "2026-02-01")):
    out = {}
    for v in versions:
        dest = tmp_path / "r2" / "d" / "t" / "v" / v / "data.parquet"
        dest.parent.mkdir(parents=True)
        out[("t", v)] = _parquet(dest, fields, rows)
    return out


def test_write_covers_what_d1_holds_and_keeps_current_rollups(tmp_path):
    fields, rows = _wide()
    store = FakeStore(_versions(tmp_path, fields, rows))
    held = {"t": {"2026-01-01": len(rows), "2026-02-01": len(rows)}}
    out = tmp_path / "out"
    got, keep = rollup.write([_ds(fields)], held, store, out, log=lambda *_: None)
    assert [w.key for w in got] == ["_rollup/t/2026-01-01.json.gz", "_rollup/t/2026-02-01.json.gz"]
    assert keep == {w.key for w in got}
    # A version this deploy did not build is read from R2: the backfill.
    assert store.fetched == [("t", "2026-01-01"), ("t", "2026-02-01")]
    # Stamped with the bytes they were built from, both stay as they are on the next deploy.
    store.stamps = {w.key: w.parquet for w in got}
    again, keep2 = rollup.write([_ds(fields)], held, store, out, log=lambda *_: None)
    assert again == [] and keep2 == keep
    # A version D1 no longer holds loses its rollup, and a database has none.
    _, keep3 = rollup.write(
        [_ds(fields)], {"t": {"2026-02-01": len(rows)}}, store, out, log=lambda *_: None
    )
    assert keep3 == {"_rollup/t/2026-02-01.json.gz"}
    assert rollup.write([_ds(fields, kind="database")], held, store, out) == ([], set())
    # A small version is not read at all.
    assert rollup.write([_ds(fields)], {"t": {"2026-01-01": 10}}, store, out) == ([], set())


def test_write_rebuilds_a_rollup_whose_parquet_changed_or_is_replaced(tmp_path):
    fields, rows = _wide()
    pq = _versions(tmp_path, fields, rows)
    store = FakeStore(pq)
    held = {"t": {"2026-01-01": len(rows), "2026-02-01": len(rows)}}
    got, _ = rollup.write([_ds(fields)], held, store, tmp_path / "a", log=lambda *_: None)
    store.stamps = {w.key: w.parquet for w in got}
    # A rebuild under --replace publishes different rows for one version.
    _parquet(pq[("t", "2026-01-01")], fields, [(*r[:4], r[4] + 1) for r in rows])
    got, _ = rollup.write([_ds(fields)], held, store, tmp_path / "b", log=lambda *_: None)
    assert [w.key for w in got] == ["_rollup/t/2026-01-01.json.gz"]
    assert got[0].parquet == rollup.file_identity(pq[("t", "2026-01-01")])
    assert got[0].parquet != store.stamps[got[0].key]
    # A replace prefix writes its version again even when the stamp still matches.
    store.stamps = {rollup.key("t", v): rollup.file_identity(p) for (_, v), p in pq.items()}
    got, _ = rollup.write(
        [_ds(fields)],
        held,
        store,
        tmp_path / "c",
        replace=["d/t/v/2026-02-01/"],
        log=lambda *_: None,
    )
    assert [w.key for w in got] == ["_rollup/t/2026-02-01.json.gz"]


def test_write_reads_a_built_tree_only_when_it_holds_the_published_bytes(tmp_path):
    fields, rows = _wide()
    pq = _versions(tmp_path, fields, rows, ("2026-01-01",))
    store = FakeStore(pq)
    held = {"t": {"2026-01-01": len(rows)}}
    dist = tmp_path / "dist"
    local = dist / "d" / "t" / "v" / "2026-01-01" / "data.parquet"
    local.parent.mkdir(parents=True)
    local.write_bytes(pq[("t", "2026-01-01")].read_bytes())
    rollup.write([_ds(fields)], held, store, tmp_path / "a", [dist], log=lambda *_: None)
    assert store.fetched == []
    # A local rebuild that R2 kept the old bytes of is not what the answer cites.
    _parquet(local, fields, rows[:6000])
    got, _ = rollup.write([_ds(fields)], held, store, tmp_path / "b", [dist], log=lambda *_: None)
    assert store.fetched == [("t", "2026-01-01")]
    assert json.loads(gzip.decompress(got[0].path.read_bytes()))["rows"] == len(rows)


def test_write_gives_a_version_no_cube_fits_an_empty_rollup(tmp_path):
    fields = [{"name": "id", "type": "integer"}, {"name": "n", "type": "integer"}]
    rows = [(i, i) for i in range(6000)]
    store = FakeStore(_versions(tmp_path, fields, rows, ("2026-01-01",)))
    got, keep = rollup.write(
        [_ds(fields)], {"t": {"2026-01-01": 6000}}, store, tmp_path / "o", log=lambda *_: None
    )
    assert keep == {got[0].key}
    assert json.loads(gzip.decompress(got[0].path.read_bytes()))["cubes"] == []


def test_write_builds_each_version_from_its_own_schema(tmp_path):
    # D1 holds a version built before the register added a field and retyped another.
    fields, rows = _wide()
    store = FakeStore(_versions(tmp_path, fields, rows, ("2026-01-01",)))
    now = [
        *({**f, "type": "integer"} if f["name"] == "kind" else f for f in fields),
        {"name": "added", "type": "string"},
    ]
    held = {"t": {"2026-01-01": len(rows)}}
    got, _ = rollup.write([_ds(now)], held, store, tmp_path / "a", log=lambda *_: None)
    obj = json.loads(gzip.decompress(got[0].path.read_bytes()))
    assert obj["fields"] == fields
    assert any("kind" in c["dims"] for c in obj["cubes"])
    # The field list D1 validates the version's queries against wins over the register.
    stated = [{"name": "region", "type": "string"}, {"name": "n", "type": "number"}]
    got, _ = rollup.write(
        [_ds(now)],
        held,
        store,
        tmp_path / "b",
        log=lambda *_: None,
        fields={("t", "2026-01-01"): stated},
    )
    obj = json.loads(gzip.decompress(got[0].path.read_bytes()))
    assert obj["fields"] == stated
    assert {d for c in obj["cubes"] for d in c["dims"]} <= {"region", "n"}


def test_version_fields_type_a_column_by_what_it_holds():
    cols = {"a": "VARCHAR", "b": "BIGINT", "c": "DOUBLE", "d": "DATE", "e": "STRUCT(x INTEGER)"}
    stated = [
        {"name": "a", "type": "integer"},
        {"name": "b", "type": "number"},
        {"name": "c", "type": "integer"},
        {"name": "d", "type": "date"},
        {"name": "e", "type": "string"},
        {"name": "f", "type": "string"},
    ]
    assert [(f.name, f.type) for f in rollup.version_fields(stated, cols)] == [
        ("a", "string"),
        ("b", "number"),
        ("c", "number"),
        ("d", "date"),
    ]


def test_write_skips_a_version_that_fails_and_writes_the_rest(tmp_path):
    fields, rows = _wide()
    pq = _versions(tmp_path, fields, rows)

    class Flaky(FakeStore):
        def fetch(self, slug, version, dest):
            if version == "2026-01-01":
                raise OSError("connection reset")
            super().fetch(slug, version, dest)

    store = Flaky(pq)
    held = {"t": {"2026-01-01": len(rows), "2026-02-01": len(rows)}}
    logs = []
    got, keep = rollup.write([_ds(fields)], held, store, tmp_path / "a", log=logs.append)
    assert [w.key for w in got] == ["_rollup/t/2026-02-01.json.gz"]
    assert keep == {"_rollup/t/2026-02-01.json.gz"}
    assert any("2026-01-01 failed" in m for m in logs)
    # A replace that fails keeps the rollup still built from the published bytes.
    k = rollup.key("t", "2026-01-01")
    store.stamps = {k: rollup.file_identity(pq[("t", "2026-01-01")])}
    got, keep = rollup.write(
        [_ds(fields)], held, store, tmp_path / "b", replace=["d/t/"], log=lambda *_: None
    )
    assert k in keep and k not in {w.key for w in got}


def test_held_fields_reads_d1_rows():
    f = [{"name": "a", "type": "string"}]
    rows = [
        {"slug": "t", "version": "2026-01-01", "fields": json.dumps(f)},
        {"slug": "t", "version": "2026-02-01"},
    ]
    assert rollup.held_fields(rows) == {("t", "2026-01-01"): f}


def _floats(n=8_000, nan=float("nan")):
    fields = [{"name": "kind", "type": "string"}, {"name": "x", "type": "number"}]
    rows = [(f"k{i % 4}", nan if i % 97 == 0 else 0.1 * (i % 13) + 1e-7 * i) for i in range(n)]
    return fields, rows


def test_float_totals_are_the_same_on_every_build_and_nan_is_null(tmp_path):
    fields, rows = _floats()
    path = _parquet(tmp_path / "f.parquet", fields, rows)
    p = rollup.Plan((("kind",),), ("x",), ())
    bodies = set()
    for _ in range(5):
        run, con = rollup.parquet_run(path)
        bodies.add(rollup.build(_ds(fields), run, p, {}, "t", "v"))
        con.close()
    assert len(bodies) == 1
    cube = json.loads(gzip.decompress(bodies.pop()))["cubes"][0]
    nans = [
        sum(1 for i, r in enumerate(rows) if r[0] == k and i % 97 == 0) for k in cube["values"][0]
    ]
    assert [
        len([r for r in rows if r[0] == k]) - z
        for k, z in zip(cube["values"][0], nans, strict=True)
    ] == cube["metrics"]["x"]["n"]


def test_write_skips_a_version_whose_totals_have_no_json_form(tmp_path):
    fields, rows = _floats(nan=float("inf"))
    store = FakeStore(_versions(tmp_path, fields, rows, ("2026-01-01",)))
    logs = []
    got, keep = rollup.write(
        [_ds(fields)], {"t": {"2026-01-01": len(rows)}}, store, tmp_path / "o", log=logs.append
    )
    assert got == [] and keep == set()
    assert any("has no rollup" in m for m in logs)


def test_held_reads_d1_rows():
    rows = [
        {"slug": "t", "version": "2026-01-01", "rows": 7},
        {"slug": "t", "version": "2026-02-01", "rows": None},
    ]
    assert rollup.held(rows) == {"t": {"2026-01-01": 7, "2026-02-01": 0}}


def test_the_command_pushes_new_rollups_and_deletes_those_d1_dropped(tmp_path, monkeypatch):
    from publicdata import r2, register
    from publicdata.__main__ import main

    fields, rows = _wide()
    pq = _versions(tmp_path, fields, rows, ("2026-02-01",))
    calls = {"put": [], "delete": []}

    class Store(FakeStore):
        def __init__(self, s3, bucket):
            super().__init__(pq, {"_rollup/t/2025-01-01.json.gz": "sha256:x"})

        def keys(self):
            return {"_rollup/t/2025-01-01.json.gz", "_rollup/gone/2026-01-01.json.gz"}

        def put(self, w):
            calls["put"].append((w.key, w.parquet))

        def delete(self, keys):
            calls["delete"].append(list(keys))

    monkeypatch.setattr(r2, "client", lambda: None)
    monkeypatch.setattr(rollup, "R2Store", Store)
    monkeypatch.setattr(register, "load", lambda _: [_ds(fields, status="live")])
    held = tmp_path / "held.json"
    # As wrangler prints it.
    held.write_text(
        json.dumps([{"results": [{"slug": "t", "version": "2026-02-01", "rows": len(rows)}]}])
    )
    assert main(["rollup", "--loaded", str(held), "--out", str(tmp_path / "o")]) == 0
    assert calls["put"] == [
        ("_rollup/t/2026-02-01.json.gz", rollup.file_identity(pq[("t", "2026-02-01")]))
    ]
    assert calls["delete"] == [["_rollup/gone/2026-01-01.json.gz", "_rollup/t/2025-01-01.json.gz"]]
    assert (
        main(["rollup", "--loaded", str(held), "--out", str(tmp_path / "o"), "--replace", "x/"])
        == 2
    )


def test_rollups_do_not_key_the_build_cache():
    assert Path(rollup.__file__).resolve() not in cache.code_files()
