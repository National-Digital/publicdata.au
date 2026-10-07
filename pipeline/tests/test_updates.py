"""Update classes and periods, over two fixture datasets: test-rolling, a rolling table with a
volatile column split by year, and test-feed, a feed split by year. Each fixture is a folder of
the files the publisher sent on each day, read in order into a fresh store by classes_fixture,
as the determinism job reads them too."""

import datetime as dt
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from publicdata import classes_fixture, fetch, gate, parts, periods, store, updates
from publicdata.build import build_dataset, version_keys
from publicdata.register import Period, RegisterError, load, parse

FIX = classes_fixture.ROOT
ROLLING_DAYS = ("2026-08-04", "2026-08-11", "2026-08-18", "2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22")  # fmt: skip
FEED_DAYS = ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06")


def registered():
    return {d.slug: d for d in load(classes_fixture.REGISTER)}


@pytest.fixture(scope="module")
def fetched(tmp_path_factory):
    """Every fixture day read, and what each read returned."""
    st = tmp_path_factory.mktemp("classes") / "store"
    return st, classes_fixture.make(st)


@pytest.fixture(scope="module")
def built(fetched, tmp_path_factory):
    st, _ = fetched
    out = tmp_path_factory.mktemp("classes-out") / "dist"
    outs = {ds.slug: build_dataset(ds, st, out) for ds in registered().values()}
    return out, outs


def tree(out: Path, slug: str, version: str, latest: bool = False) -> Path:
    return out / "d" / slug / ("fetch" if latest else "v") / version


def manifest(out: Path, slug: str, version: str, latest: bool = False) -> dict:
    return json.loads((tree(out, slug, version, latest) / "manifest.json").read_text("utf-8"))


def log_of(st: Path, slug: str, day: str) -> dict:
    return json.loads((st / slug / day / "changes.json").read_text(encoding="utf-8"))


# Register


def entry(**over):
    raw = {
        "slug": "tt",
        "title": "T",
        "status": "backlog",
        "publisher": {"name": "P", "jurisdiction": "Qld"},
        "licence": {"id": "CC-BY-4.0"},
        "source": {"url": "https://example.gov.au/x.csv"},
        "key": ["id"],
        "fields": [
            {"name": "id", "source": "ID"},
            {"name": "day", "source": "Day", "type": "date"},
            {"name": "year", "source": "Year", "type": "integer"},
            {"name": "stamp", "source": "Stamp", "type": "datetime"},
        ],
    }
    raw.update(over)
    return parse(raw, "tt.yaml")


def test_a_release_is_the_default_and_carries_no_period():
    d = entry()
    assert (d.update, d.volatile, d.period) == ("release", (), None)


@pytest.mark.parametrize(
    "over, words",
    [
        ({"update": "weekly"}, "update 'weekly'"),
        ({"update": "rolling", "key": []}, "needs a key"),
        ({"update": "feed"}, "needs a period"),
        ({"update": "feed", "source": {"url": "u", "feed": True}}, "replaces source.feed"),
        ({"volatile": ["stamp"]}, "rolling source or a feed"),
        ({"update": "rolling", "volatile": ["nope"]}, "not a declared field"),
        ({"update": "rolling", "volatile": ["id"]}, "part of the key"),
        ({"period": {"field": "day", "grain": "week"}}, "grain 'week'"),
        ({"period": {"field": "id", "grain": "year"}}, "date or datetime"),
        ({"period": {"field": "year", "grain": "month"}}, "integer year for grain year"),
        ({"period": {"field": "year", "grain": "fiscal"}}, "integer year for grain year"),
        ({"period": {"field": "day", "grain": "year", "revision_window": 0}}, "at least 1"),
        ({"period": {"field": "day", "grain": "year", "size": 1}}, "period takes"),
    ],
)
def test_register_refuses_a_class_or_period_it_cannot_follow(over, words):
    with pytest.raises(RegisterError, match=words):
        entry(**over)


def test_the_fixture_entries_parse():
    r = registered()
    assert r["test-rolling"].update == "rolling" and r["test-rolling"].volatile == ("updated_at",)
    assert r["test-feed"].period == Period("reported", "year", 2)
    assert entry(period={"field": "year", "grain": "year"}).period.grain == "year"
    assert entry(period={"field": "day", "grain": "fiscal"}).period.grain == "fiscal"


def test_a_manifest_written_before_the_classes_keeps_its_bytes(fixture_store):
    for p in fixture_store.glob("*/*/manifest.json"):
        assert store.Manifest.read(p).to_json() == p.read_text(encoding="utf-8")


def test_a_period_added_to_an_entry_leaves_its_versions_alone(fixture_store, tmp_path):
    """The period is read from each version's manifest, which a fetch writes, so giving an entry
    with history a period changes no version it already has."""
    reg = Path(__file__).parents[2] / "register"
    ds = next(d for d in load(reg) if d.slug == "qld-road-casualties")
    date = next(f.name for f in ds.fields if f.type in ("date", "integer"))
    grain = "year"
    split = replace(ds, period=Period(date, grain))
    a, b = tmp_path / "a", tmp_path / "b"
    build_dataset(ds, fixture_store, a)
    build_dataset(split, fixture_store, b)
    files = sorted(p.relative_to(a) for p in (a / "d").rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(b) for p in (b / "d").rglob("*") if p.is_file())
    for rel in files:
        if rel.name != "data.duckdb":
            assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel
    assert repr(ds) == repr(split)


# Periods


def test_period_labels_and_the_undated_part():
    col = pa.chunked_array([pa.array([dt.date(2025, 2, 3), None, dt.date(2026, 12, 1)])])
    assert periods.labels(col, "year").to_pylist() == ["2025", "undated", "2026"]
    assert periods.labels(col, "quarter").to_pylist() == ["2025-Q1", "undated", "2026-Q4"]
    assert periods.labels(col, "month").to_pylist() == ["2025-02", "undated", "2026-12"]
    assert periods.labels(col, "fiscal").to_pylist() == ["2024-25", "undated", "2026-27"]
    years = pa.chunked_array([pa.array([2019, None, 99], pa.int64())])
    assert periods.labels(years, "year").to_pylist() == ["2019", "undated", "0099"]
    assert periods.ordered(["undated", "2026", "0099", "2025"]) == [
        "0099",
        "2025",
        "2026",
        "undated",
    ]


def test_the_revision_window_is_the_current_period_and_the_one_before():
    day = dt.date(2026, 1, 10)
    per = Period("d", "month", 2)
    assert periods.open_since(day, per) == "2025-12"
    assert periods.finished("2025-11", day, per) and not periods.finished("2025-12", day, per)
    assert periods.open_since(day, Period("d", "quarter", 3)) == "2025-Q3"
    assert periods.open_since(day, Period("d", "fiscal", 2)) == "2024-25"
    assert periods.finished("2023-24", day, Period("d", "fiscal", 2))
    assert periods.of_day(dt.date(2026, 7, 1), "fiscal") == "2026-27"
    assert not periods.finished(periods.UNDATED, day, per)


def test_part_sizing_takes_the_largest_grain_under_the_limit():
    mb = 1024 * 1024
    assert periods.grain_problem({"2025": 120 * mb}, "year") == (
        f"part 2025 is {120 * mb} bytes, over {periods.PART_MAX}; use grain quarter"
    )
    assert "use grain quarter" in periods.grain_problem({"2025-26": 120 * mb}, "fiscal")
    assert "use grain month" in periods.grain_problem({"2025-Q1": 101 * mb}, "quarter")
    small = {"2025-01": 30 * mb, "2025-02": 30 * mb, "2025-03": 30 * mb}
    assert periods.grain_problem(small, "month") == (
        f"grain quarter keeps every part under {periods.PART_MAX} bytes; use it"
    )
    big = {"2025-01": 60 * mb, "2025-02": 60 * mb}
    assert periods.grain_problem(big, "month") == ""
    assert periods.grain_problem({"2025": 1, "undated": 1}, "year") == ""


# Snapshot rules


def snap(version: str) -> store.Manifest:
    return store.Manifest("t", version, "", "", "", 0, "t.csv", "utf-8", {}, {})


@pytest.mark.parametrize(
    "log, last, day, why, period",
    [
        (None, None, "2026-09-01", "first", None),
        ({"added": 0, "removed": 0, "changed": 1, "rows_from": 100, "revised": ["2020"]}, "2026-09-01", "2026-09-02", "revision", Period("d", "year")),
        ({"added": 6, "removed": 0, "changed": 0, "rows_from": 100}, "2026-09-01", "2026-09-02", "churn", None),
        ({"added": 5, "removed": 0, "changed": 0, "rows_from": 100}, "2026-09-01", "2026-09-02", "", None),
        ({"added": 0, "removed": 0, "changed": 1, "rows_from": 100}, "2026-09-30", "2026-10-01", "monthly", None),
        ({"added": 0, "removed": 0, "changed": 1, "rows_from": 100}, "2026-12-30", "2027-01-02", "period-close", Period("d", "year")),
        (None, "2026-09-30", "2026-10-07", "monthly", None),
        (None, "2026-10-01", "2026-10-07", "", None),
    ],
)  # fmt: skip
def test_snapshot_cutting_rules(log, last, day, why, period):
    ds = replace(entry(), period=period)
    snaps = [snap(last)] if last else []
    assert updates.cut(ds, log, snaps, dt.date.fromisoformat(day)) == why


def cuts(got, slug, days):
    def one(day):
        m = got[(slug, day)]
        return None if m is None else (m.cut if m.snapshot else "fetch")

    return [one(d) for d in days]


def test_each_fixture_fetch_is_a_snapshot_only_when_a_rule_says_so(fetched):
    _, got = fetched
    assert cuts(got, "test-rolling", ROLLING_DAYS) == [
        "first", "fetch", None, "monthly", "revision", "churn", "fetch"
    ]  # fmt: skip
    # The empty read and the read after it each change every row; the last read changes none.
    assert cuts(got, "test-feed", FEED_DAYS) == ["first", "churn", "churn", "churn", "churn", None]


def test_an_unchanged_read_in_a_new_month_keeps_the_newest_fetch_where_it_stands(tmp_path):
    ds = registered()["test-rolling"]
    days = FIX / "test-rolling"
    for name in ("2026-08-04", "2026-08-11"):
        classes_fixture.read(ds, tmp_path, days / f"{name}.csv")
    again = tmp_path / "in" / "2026-09-02.csv"
    again.parent.mkdir()
    shutil.copyfile(days / "2026-08-11.csv", again)
    m = classes_fixture.read(ds, tmp_path, again)
    # Nothing is dated again: the August fetch becomes the snapshot under its own date.
    # Judged by its own date: it was the last change of August.
    assert (m.version, m.snapshot, m.cut) == ("2026-08-11", True, "month-end")
    assert not (tmp_path / "test-rolling" / "2026-09-02").exists()
    on_disk = store.Manifest.read(tmp_path / "test-rolling" / "2026-08-11" / "manifest.json")
    assert (on_disk.snapshot, on_disk.cut, on_disk.sha256) == (True, "month-end", m.sha256)
    assert "2026-09-02" in on_disk.notes[-1]
    # The change log, published when the fetch was, is left as it was.
    assert log_of(tmp_path, "test-rolling", "2026-08-11")["snapshot"] == ""
    later = tmp_path / "in" / "2026-09-09.csv"
    shutil.copyfile(again, later)
    assert classes_fixture.read(ds, tmp_path, later) is None


def test_a_rolling_fetch_without_the_newest_bytes_says_how_to_get_them(tmp_path):
    ds = registered()["test-rolling"]
    m = classes_fixture.read(ds, tmp_path, FIX / "test-rolling" / "2026-08-04.csv")
    store.source_path(tmp_path, m).unlink()
    with pytest.raises(fetch.FetchError, match="store pull --rolling"):
        classes_fixture.read(ds, tmp_path, FIX / "test-rolling" / "2026-08-11.csv")


def test_each_fetch_records_the_period_it_is_split_by(fetched):
    st, _ = fetched
    for m in store.manifests(st, "test-rolling", fetches=True):
        assert m.period == {"field": "opened", "grain": "year", "revision_window": 2}


# Volatile columns


def test_volatile_columns_alone_are_no_change(fetched):
    st, got = fetched
    assert got[("test-rolling", "2026-08-18")] is None
    assert not (st / "test-rolling" / "2026-08-18").exists()
    log = log_of(st, "test-rolling", "2026-08-11")
    assert (log["changed"], log["changed_keys"], log["volatile"]) == (1, [40], ["updated_at"])
    assert log["examples"][0]["fields"] == {
        "name": {"from": "Premises 40", "to": "Premises 40 (renamed)"}
    }


def test_volatile_columns_are_part_of_the_cache_key(fetched, tmp_path):
    from publicdata.cache import BuildCache

    st, _ = fetched
    cache = BuildCache(tmp_path / "cache")
    ds = registered()["test-rolling"]
    keys = [k for _, k in version_keys(cache, ds, st)]
    assert keys != [k for _, k in version_keys(cache, replace(ds, volatile=()), st)]


# Revisions and parts


def test_a_change_to_a_finished_period_is_flagged_as_a_revision(fetched, built):
    st, _ = fetched
    out, _ = built
    log = log_of(st, "test-rolling", "2026-09-08")
    assert (log["periods"], log["revised"], log["snapshot"]) == (["2022"], ["2022"], "revision")
    m = manifest(out, "test-rolling", "2026-09-08")
    # The manifest names the revisions the change logs since the snapshot before name.
    assert (m["cut"], m["revised"]) == ("revision", ["2022"])
    by = {p["period"]: p for p in m["parts"]}
    assert by["2022"]["revised"] and by["2022"]["tree"] == "2026-09-08"
    assert [p for p, r in by.items() if r["revised"]] == ["2022"]
    assert (tree(out, "test-rolling", "2026-09-08") / "parts/2022.parquet").is_file()
    later = {p["period"]: p for p in manifest(out, "test-rolling", "2026-09-15")["parts"]}
    assert later["2022"]["tree"] == "2026-09-08" and not later["2022"]["revised"]


def test_a_finished_part_is_written_once_and_reused(built):
    out, _ = built
    for v in ("2026-09-01", "2026-09-08", "2026-09-15"):
        by = {p["period"]: p for p in manifest(out, "test-rolling", v)["parts"]}
        assert by["2023"]["tree"] == by["2024"]["tree"] == "2026-08-04"
        assert by["2025"]["tree"] == by["2026"]["tree"] == v
        assert not (tree(out, "test-rolling", v) / "parts/2023.parquet").exists()
    first = {p["period"]: p for p in manifest(out, "test-rolling", "2026-08-04")["parts"]}
    assert [k for k, p in first.items() if p["finished"]] == ["2022", "2023", "2024"]


def test_a_reused_part_holds_the_same_rows_the_whole_table_does(built):
    out, _ = built
    vdir = tree(out, "test-rolling", "2026-09-15")
    whole = pq.read_table(vdir / "data.parquet").to_pylist()
    m = manifest(out, "test-rolling", "2026-09-15")
    rows = []
    for r in m["parts"]:
        src = out / "d" / "test-rolling" / "v" / r["tree"] / r["files"]["parquet"]["path"]
        rows += pq.read_table(src).to_pylist()
    key = lambda r: r["id"]  # noqa: E731
    assert sorted(rows, key=key) == sorted(whole, key=key)


def test_a_finished_part_is_not_reused_when_its_column_types_differ(fetched, built, tmp_path):
    from publicdata.normalise import normalise

    st, _ = fetched
    out, _ = built
    m = manifest(out, "test-rolling", "2026-09-15")
    ds = registered()["test-rolling"]
    sm = store.manifests(st, "test-rolling")[-1]
    tbl = normalise(ds, sm, store.source_path(st, sm).read_bytes())
    per = Period("opened", "year")

    def write(prior):
        recs, _ = parts.write(
            tbl, tbl.table, per, lambda n, r: {}, tmp_path, "x", prior, revised=set()
        )
        return {r["period"]: r["tree"] for r in recs}["2023"]

    assert write(m["parts"]) == "2026-08-04"
    assert write([{**r, "schema": "written as int64"} for r in m["parts"]]) == "x"


def test_rows_without_a_date_are_the_undated_part_which_never_finishes(fetched, built):
    st, got = fetched
    out, _ = built
    m = manifest(out, "test-rolling", "2026-09-15")
    undated = next(p for p in m["parts"] if p["period"] == periods.UNDATED)
    assert (undated["rows"], undated["finished"]) == (2, False)
    t = pq.read_table(tree(out, "test-rolling", "2026-09-15") / "parts/undated.parquet")
    assert t.column("opened").null_count == 2
    assert got[("test-rolling", "2026-09-22")].snapshot is False
    log = log_of(st, "test-rolling", "2026-09-22")
    assert (log["periods"], log["revised"]) == (["undated"], [])


def test_the_parts_hold_every_row_once(built):
    out, _ = built
    m = manifest(out, "test-rolling", "2026-09-15")
    assert sum(p["rows"] for p in m["parts"]) == m["rows"] == 47
    vdir = tree(out, "test-rolling", "2026-09-15")
    assert m["whole"] is True and (vdir / "data.parquet").is_file()
    head = json.loads(pq.read_schema(vdir / "parts/2025.parquet").metadata[b"publicdata"])
    assert head["period"] == {"field": "opened", "grain": "year", "value": "2025"}


def test_a_table_too_large_for_one_file_is_its_parts_and_a_duckdb_file(
    fetched, tmp_path, monkeypatch
):
    st, _ = fetched
    monkeypatch.setattr(parts, "WHOLE_BYTES", 0)
    out = tmp_path / "dist"
    o = build_dataset(registered()["test-rolling"], st, out)
    vdir = tree(out, "test-rolling", "2026-09-15")
    assert not (vdir / "data.parquet").exists() and not (vdir / "data.csv").exists()
    assert (vdir / "data.duckdb").is_file() and o.latest.whole is False
    import duckdb

    con = duckdb.connect(str(vdir / "data.duckdb"), read_only=True)
    urls = [r[0] for r in con.execute("SELECT url FROM parts ORDER BY period").fetchall()]
    assert urls[1] == "https://publicdata.au/d/test-rolling/v/2026-08-04/parts/2023.parquet"
    macro = con.execute(
        "SELECT macro_definition FROM duckdb_functions() WHERE function_name = 'records'"
    ).fetchone()
    assert macro == ("SELECT * FROM read_parquet(files)",)
    con.close()
    d = json.loads((out / "d/test-rolling/diff/2026-09-08..2026-09-15.json").read_text("utf-8"))
    assert d["added"] == 5
    dp = json.loads((out / "d/test-rolling/datapackage.json").read_text("utf-8"))
    assert [r["name"] for r in dp["resources"]][:3] == ["duckdb", "part-2022", "part-2023"]


def test_a_parts_only_version_says_what_it_leaves_out_and_lists_only_its_files(
    fetched, tmp_path, monkeypatch
):
    from publicdata.site import render_site

    st, _ = fetched
    monkeypatch.setattr(parts, "WHOLE_BYTES", 0)
    out = tmp_path / "dist"
    render_site([build_dataset(ds, st, out) for ds in registered().values()], out)
    page = (out / "d/test-rolling/index.html").read_text("utf-8")
    assert "data-no-query" in page and "split by year and is too large to be one file" in page
    assert not (out / "d/test-rolling/in").exists()
    llms = (out / "llms.txt").read_text("utf-8")
    line = next(x for x in llms.splitlines() if "/d/test-rolling/" in x)
    assert "data.duckdb" in line and "data.csv" not in line and "data.parquet" not in line
    assert gate.query_explained(out, registered()) == []
    bare = page.replace("data-no-query", "")
    (out / "d/test-rolling/index.html").write_text(bare, encoding="utf-8")
    assert "says no reason" in gate.query_explained(out, registered())[0]


def test_the_gate_holds_a_grain_to_its_part_size(built, tmp_path, monkeypatch):
    out, _ = built
    site = tmp_path / "site"
    shutil.copytree(out, site)
    (site / "latest.json").write_text(json.dumps({"test-rolling": "2026-09-15"}))
    (site / "catalog.json").write_text(json.dumps({"dataset": []}))
    reg = registered()
    assert gate.periods_needed(site, reg) == []
    monkeypatch.setattr(periods, "PART_MAX", 1000)
    assert "use grain quarter" in gate.periods_needed(site, reg)[0]


def test_the_gate_asks_a_large_dated_table_for_a_period(built, tmp_path, monkeypatch):
    out, _ = built
    site = tmp_path / "site"
    shutil.copytree(out, site)
    (site / "latest.json").write_text(json.dumps({"test-rolling": "2026-09-15"}))
    cat = {"dataset": [{"identifier": "test-rolling", "distribution": [{"format": "parquet", "byteSize": 200 * 1024 * 1024}]}]}  # fmt: skip
    (site / "catalog.json").write_text(json.dumps(cat))
    reg = {"test-rolling": replace(registered()["test-rolling"], period=None)}
    assert "need a period" in gate.periods_needed(site, reg)[0]
    # A period in the register settles it at once, from the next fetch on.
    assert gate.periods_needed(site, registered()) == []
    monkeypatch.setitem(gate.PERIOD_PENDING, "test-rolling", "to be split")
    assert gate.periods_needed(site, reg) == []


# latest/ and the change log


def test_latest_is_the_newest_fetch_built_whole_in_a_folder_of_its_own(built):
    out, outs = built
    o = outs["test-rolling"]
    assert o.latest.manifest.version == "2026-09-15"
    assert o.current.manifest.version == "2026-09-22"
    m = manifest(out, "test-rolling", "2026-09-22", latest=True)
    assert m["url"] == "https://publicdata.au/d/test-rolling/latest/"
    assert (m["snapshot"], m.get("cut")) == (False, None)
    rows = (tree(out, "test-rolling", "2026-09-22", True) / "data.csv").read_text("utf-8")
    assert "Premises 41 (renamed)" in rows
    snap_rows = (tree(out, "test-rolling", "2026-09-15") / "data.csv").read_text("utf-8")
    assert "Premises 41 (renamed)" not in snap_rows
    by = {p["period"]: p["tree"] for p in m["parts"]}
    assert by == {"2022": "2026-09-08", "2023": "2026-08-04", "2024": "2026-08-04",
                  "2025": "latest", "2026": "latest", "undated": "latest"}  # fmt: skip
    versions = json.loads((out / "d/test-rolling/versions.json").read_text("utf-8"))
    assert versions["update"] == "rolling" and versions["latest"] == "2026-09-15"
    assert [v.get("cut") for v in versions["versions"]] == ["first", "monthly", "revision", "churn"]


def test_every_fetch_has_its_change_log(built):
    out, _ = built
    idx = json.loads((out / "d/test-rolling/changes/index.json").read_text("utf-8"))
    assert idx["latest_fetch"] == "2026-09-22" and idx["snapshot"] == "2026-09-15"
    assert [(f["fetch"], f["snapshot"]) for f in idx["fetches"]] == [
        ("2026-08-04", "first"),
        ("2026-08-11", None),
        ("2026-09-01", "monthly"),
        ("2026-09-08", "revision"),
        ("2026-09-15", "churn"),
        ("2026-09-22", None),
    ]
    churn = json.loads((out / "d/test-rolling/changes/2026-09-15.json").read_text("utf-8"))
    assert (churn["added"], churn["added_keys"]) == (5, [43, 44, 45, 46, 47])
    assert gate.fetch_sequence(out) == []


def test_the_gate_catches_a_fetch_compared_with_one_that_is_not_the_one_before(built, tmp_path):
    out, _ = built
    site = tmp_path / "site"
    shutil.copytree(out, site)
    p = site / "d/test-rolling/changes/index.json"
    idx = json.loads(p.read_text("utf-8"))
    idx["fetches"][2]["from"] = "2026-08-04"
    p.write_text(json.dumps(idx))
    assert "compared with 2026-08-04, not 2026-08-11" in gate.fetch_sequence(site)[0]


def test_a_release_has_no_latest_tree_or_change_log(fixture_store, tmp_path):
    ds = next(d for d in load(Path(__file__).parents[2] / "register") if d.slug == "abs-lga-2025")
    o = build_dataset(ds, fixture_store, tmp_path)
    assert o.current is None and o.fetches == []
    assert not (tmp_path / "d/abs-lga-2025/fetch").exists()
    assert not (tmp_path / "d/abs-lga-2025/changes").exists()
    assert "update" not in json.loads((tmp_path / "d/abs-lga-2025/versions.json").read_text())


# The feed's history


def seen_rows(t: pa.Table) -> list[tuple]:
    return [(r["id"], str(r["first_seen"]), str(r["last_seen"])) for r in t.to_pylist()]


def test_a_feed_keeps_every_state_with_the_first_and_last_read_that_held_it(fetched, built):
    st, _ = fetched
    out, _ = built
    last = store.manifests(st, "test-feed", fetches=True)[-1]
    assert last.version == "2026-10-05"
    assert seen_rows(pq.read_table(store.history_path(st, last))) == [
        ("A", "2026-10-01", "2026-10-03"),
        ("A", "2026-10-05", "2026-10-05"),
        ("B", "2026-10-01", "2026-10-01"),
        ("B", "2026-10-03", "2026-10-03"),
        ("C", "2026-10-01", "2026-10-03"),
        ("D", "2026-10-02", "2026-10-03"),
    ]
    m = manifest(out, "test-feed", "2026-10-03")
    assert [p["period"] for p in m["history"]["parts"]] == ["2025", "2026", "undated"]
    assert not any(p["revised"] for p in m["history"]["parts"])
    t = pq.read_table(tree(out, "test-feed", "2026-10-03") / "history/2026.parquet")
    assert t.column_names[-2:] == ["first_seen", "last_seen"] and t.num_rows == 3


def test_an_empty_feed_is_a_state_that_closes_every_row(fetched, built):
    st, _ = fetched
    out, _ = built
    log = log_of(st, "test-feed", "2026-10-04")
    assert (log["removed"], log["rows_to"], log["snapshot"]) == (4, 0, "churn")
    assert manifest(out, "test-feed", "2026-10-04")["rows"] == 0


def test_a_feed_records_every_read_and_latest_carries_last_seen_to_it(fetched, built):
    st, got = fetched
    out, _ = built
    assert got[("test-feed", "2026-10-06")] is None
    assert store.last_read(st, "test-feed") == {"read": "2026-10-06", "fetch": "2026-10-05"}
    latest = tree(out, "test-feed", "2026-10-05", True)
    assert seen_rows(pq.read_table(latest / "history/2025.parquet")) == [
        ("A", "2026-10-01", "2026-10-03"),
        ("A", "2026-10-05", "2026-10-06"),
    ]
    snap = tree(out, "test-feed", "2026-10-05")
    assert ("A", "2026-10-05", "2026-10-05") in seen_rows(
        pq.read_table(snap / "history/2025.parquet")
    )


def test_a_column_that_stops_being_volatile_joins_the_history_as_null(fetched):
    from publicdata.normalise import normalise

    st, _ = fetched
    ds = registered()["test-feed"]
    m = store.manifests(st, "test-feed", fetches=True)[2]
    old = pq.read_table(store.history_path(st, m)).drop_columns(["status"])
    tbl = normalise(ds, m, store.source_path(st, m).read_bytes())
    out = updates.history(old, tbl, "2026-10-04", m.version)
    assert out.column_names == ["id", "road", "status", "reported", "first_seen", "last_seen"]
    # Compared on the columns both hold, every current state carries on.
    assert out.num_rows == old.num_rows
    assert out.column("status").null_count == out.num_rows


def test_the_history_archive_holds_each_version_s_history_parts(built):
    import io
    import tarfile

    import zstandard

    out, _ = built
    raw = zstandard.ZstdDecompressor().decompress(
        (out / "d/test-feed/history.tar.zst").read_bytes(), max_output_size=1 << 26
    )
    names = tarfile.open(fileobj=io.BytesIO(raw)).getnames()
    assert "test-feed/2026-10-03/history/2026.parquet" in names


# Determinism


def test_two_builds_of_the_same_fetches_are_byte_identical(fetched, built, tmp_path):
    st, _ = fetched
    out, _ = built
    again = tmp_path / "dist"
    for ds in registered().values():
        build_dataset(ds, st, again)
    a = {p.relative_to(out): p for p in out.rglob("*") if p.is_file() and p.name != "data.duckdb"}
    b = {
        p.relative_to(again): p for p in again.rglob("*") if p.is_file() and p.name != "data.duckdb"
    }
    assert a.keys() == b.keys()
    assert [k for k in a if a[k].read_bytes() != b[k].read_bytes()] == []


def test_a_cached_build_serves_latest_whole(fetched, built, tmp_path):
    from publicdata.cache import BuildCache

    st, _ = fetched
    out, _ = built
    cache = BuildCache(tmp_path / "cache")
    ds = registered()["test-rolling"]
    build_dataset(ds, st, tmp_path / "cold", cache)
    warm = tmp_path / "warm"
    o = build_dataset(ds, st, warm, cache)
    assert o.current.absent == ()
    for p in tree(out, "test-rolling", "2026-09-22", True).rglob("*"):
        if p.is_file() and p.name != "data.duckdb":
            rel = p.relative_to(out)
            assert (warm / rel).read_bytes() == p.read_bytes(), rel


def test_the_pages_list_the_parts_and_say_how_the_source_is_followed(fetched, tmp_path):
    from publicdata.site import render_site

    st, _ = fetched
    out = tmp_path / "dist"
    render_site([build_dataset(ds, st, out) for ds in registered().values()], out)
    assert json.loads((out / "current.json").read_text("utf-8")) == {
        "test-feed": {"fetch": "2026-10-05", "source": "source.csv"},
        "test-rolling": {"fetch": "2026-09-22", "source": "source.csv"},
    }
    page = (out / "d/test-rolling/index.html").read_text("utf-8")
    assert "This site reads the whole table every week" in page
    assert 'href="https://publicdata.au/d/test-rolling/changes/index.json"' in page
    assert '<h2 id="parts">By year</h2>' in page
    assert "https://publicdata.au/d/test-rolling/v/2026-08-04/parts/2023.parquet" in page
    assert "Kept as a dated version because a finished period changed." in page
    feed = (out / "d/test-feed/index.html").read_text("utf-8")
    assert "reads the feed every day" in feed and "history/2026.parquet" in feed
    assert gate.main(out, classes_fixture.REGISTER) == 0


def test_every_part_of_a_version_has_the_same_column_types(built):
    out, _ = built
    for slug, v in (("test-rolling", "2026-09-15"), ("test-feed", "2026-10-03")):
        m = manifest(out, slug, v)
        for recs in (m["parts"], m.get("history", {}).get("parts", [])):
            types = {
                str(
                    pq.read_schema(
                        out / f"d/{slug}/v/{r['tree']}" / r["files"]["parquet"]["path"]
                    ).remove_metadata()
                )
                for r in recs
            }
            assert len(types) <= 1, (slug, types)


def test_latest_carries_last_seen_only_to_a_read_of_its_own_fetch(fetched, tmp_path):
    st, _ = fetched
    copy = tmp_path / "store"
    shutil.copytree(st, copy)
    ds = registered()["test-feed"]

    def last_seen_of_a(out):
        t = pq.read_table(tree(out, "test-feed", "2026-10-05", True) / "history/2025.parquet")
        return seen_rows(t)[-1][2]

    # The read record lives in the raw store; without it, last_seen stops at the newest fetch.
    store.read_path(copy, "test-feed").unlink()
    build_dataset(ds, copy, tmp_path / "none")
    assert last_seen_of_a(tmp_path / "none") == "2026-10-05"
    # One that names a fetch this checkout does not hold yet is not taken either.
    store.write_read(copy, "test-feed", "2026-10-09", "2026-10-08")
    build_dataset(ds, copy, tmp_path / "ahead")
    assert last_seen_of_a(tmp_path / "ahead") == "2026-10-05"
    store.write_read(copy, "test-feed", "2026-10-09", "2026-10-05")
    build_dataset(ds, copy, tmp_path / "read")
    assert last_seen_of_a(tmp_path / "read") == "2026-10-09"


def test_a_promotion_is_judged_by_the_fetch_s_own_date():
    year = Period("d", "year")
    d = dt.date.fromisoformat
    assert updates.closing(None, d("2026-08-11"), d("2026-09-02")) == "month-end"
    assert updates.closing(None, d("2026-09-01"), d("2026-09-29")) == ""
    assert updates.closing(year, d("2026-12-30"), d("2027-01-05")) == "period-end"
    assert updates.closing(year, d("2026-11-30"), d("2026-12-05")) == "month-end"


def test_each_fetch_records_its_class_and_a_version_keeps_it(fetched, tmp_path):
    from publicdata.build import revised_since, version_key
    from publicdata.cache import BuildCache

    st, _ = fetched
    assert {m.update for m in store.manifests(st, "test-rolling", fetches=True)} == {"rolling"}
    assert {m.update for m in store.manifests(st, "test-feed", fetches=True)} == {"feed"}
    ds = registered()["test-rolling"]
    m = store.manifests(st, "test-rolling")[1]
    # The register's class no longer decides how the version flags revisions.
    assert revised_since(replace(ds, update="release"), st, m) == set()
    as_release = replace(m, update="")
    assert revised_since(ds, st, as_release) is None
    cache = BuildCache(tmp_path / "cache")
    assert version_key(cache, ds, m, st, "p") != version_key(
        cache, ds, replace(m, update=""), st, "p"
    )


def test_each_fetch_records_its_volatile_columns_and_a_version_keeps_them(fetched, built, tmp_path):
    st, _ = fetched
    out, _ = built
    ms = store.manifests(st, "test-rolling", fetches=True)
    assert {tuple(m.volatile) for m in ms} == {("updated_at",)}
    assert {tuple(m.volatile) for m in store.manifests(st, "test-feed", fetches=True)} == {()}
    # The register dropping the column leaves each part's stable hash as the version recorded it.
    ds = replace(registered()["test-rolling"], update="release", volatile=())
    again = tmp_path / "dist"
    build_dataset(ds, st, again)
    for m in store.manifests(st, "test-rolling"):
        assert (
            manifest(again, "test-rolling", m.version)["parts"]
            == manifest(out, "test-rolling", m.version)["parts"]
        )


def test_a_feed_s_history_marks_first_and_last_seen_as_observed_here(built, tmp_path):
    from publicdata.site import render_site

    out, outs = built
    schema = json.loads((tree(out, "test-feed", "2026-10-03") / "history/schema.json").read_text())
    by = {f["name"]: f for f in schema["fields"]}
    for name in ("first_seen", "last_seen"):
        assert by[name]["publicdata:derived"] == {"method": "observed by publicdata.au"}
        assert by[name]["type"] == "date"
    assert "publicdata:derived" not in by["road"]
    assert schema["primaryKey"] == ["id", "first_seen"]
    assert manifest(out, "test-feed", "2026-10-03")["history"]["schema"] == "history/schema.json"
    site = tmp_path / "site"
    shutil.copytree(out, site)
    render_site(list(outs.values()), site)
    page = (site / "d/test-feed/index.html").read_text("utf-8")
    assert "the dates it first and last read the row in that state" in page
