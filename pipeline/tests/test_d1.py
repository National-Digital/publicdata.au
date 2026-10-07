import json
import sqlite3
import subprocess
import sys

import pytest

from publicdata import d1


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(d1, "ASK_DELAY", 0)


def test_literals_quote_text_and_keep_numbers():
    assert d1.literal(None) == "NULL" and d1.literal(True) == "1" and d1.literal(3) == "3"
    assert d1.literal("O'Connor") == "'O''Connor'"
    assert d1.literal(float("nan")) == "NULL"


def test_a_statement_stays_under_the_limit_in_bytes_when_text_is_not_ascii(tmp_path):
    db = tmp_path / "data.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE records (name TEXT)")
    con.execute("CREATE TABLE fields (name TEXT, type TEXT)")
    con.execute("CREATE TABLE publicdata (key TEXT, value TEXT)")
    con.execute("INSERT INTO fields VALUES ('name', 'string')")
    # Each of these letters is four bytes, so a batch sized in characters runs past the limit.
    con.executemany("INSERT INTO records VALUES (?)", [("𝓐" * 200,)] * 2000)
    con.commit()
    con.close()
    inserts = [
        s for s in d1.version_sql(db, "t", "2026-01-01", ()) if s.startswith('INSERT INTO "v_')
    ]
    assert len(inserts) > 1
    assert all(len(s.encode()) <= d1.MAX_STATEMENT for s in inserts)


def _version(tmp_path, rows):
    db = tmp_path / "data.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE records (id INTEGER, body TEXT, raw BLOB)")
    con.execute("CREATE TABLE fields (name TEXT, type TEXT)")
    con.execute("CREATE TABLE publicdata (key TEXT, value TEXT)")
    con.executemany("INSERT INTO fields VALUES (?, ?)", [("id", "integer"), ("body", "string")])
    con.executemany("INSERT INTO records VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return db


def _run(stmts):
    con = sqlite3.connect(":memory:")
    con.execute(d1.REGISTRY)
    for s in stmts:
        assert len(s.encode()) <= d1.MAX_STATEMENT
        con.execute(s)
    return con


def test_a_row_wider_than_a_statement_loads_whole_a_piece_at_a_time(tmp_path):
    # Quotes and four-byte letters, so neither an escape nor a character may split at a piece.
    body = ("It's 𝓐 heritage place. " * 12_000)[:290_000]
    raw = bytes(range(256)) * 600
    rows = [(1, "short", None), (2, body, raw), (3, "after", b"\x00")]
    stmts = list(d1.version_sql(_version(tmp_path, rows), "t", "2026-01-01", ()))
    con = _run(stmts)
    tbl = d1.table_name("t", "2026-01-01")
    assert con.execute(f'SELECT id, body, raw FROM "{tbl}" ORDER BY rowid').fetchall() == rows
    assert con.execute("SELECT rows FROM _versions").fetchone() == (3,)


def test_a_wide_row_missing_a_piece_leaves_the_version_unregistered(tmp_path):
    stmts = list(d1.version_sql(_version(tmp_path, [(1, "x" * 200_000, None)]), "t", "v", ()))
    con = _run(s for s in stmts if not (s.startswith("UPDATE") and s is stmts[-2]))
    assert con.execute("SELECT COUNT(*) FROM _versions").fetchone() == (0,)


def test_a_row_larger_than_d1_holds_keeps_the_version_files_only(tmp_path):
    db = _version(tmp_path, [(1, "x" * (d1.MAX_ROW + 1), None)])
    try:
        list(d1.version_sql(db, "t", "v", ()))
    except d1.TooWide:
        return
    raise AssertionError("expected TooWide")


def test_load_files_rebuild_the_latest_version_and_register_it(tmp_path, fixture_site):
    out = fixture_site
    loads = tmp_path / "load"
    loaded = tmp_path / "loaded.json"
    loaded.write_text(
        json.dumps(
            [
                {
                    "results": [{"slug": "qld-road-casualties", "version": "2026-04-24"}],
                    "success": True,
                }
            ]
        )
    )
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "publicdata",
            "d1",
            "sql",
            "--root",
            str(out),
            "--loaded",
            str(loaded),
            "--out",
            str(loads),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    names = sorted(p.name for p in loads.glob("*.sql"))
    assert "qld-road-crash-locations@2026-04-24.part001.sql" in names
    assert not any(n.startswith("qld-road-casualties@") for n in names)  # already loaded
    assert "d1:" in r.stdout
    db = sqlite3.connect(":memory:")
    for p in sorted(loads.glob("*.sql")):
        text = p.read_text(encoding="utf-8")
        assert max(len(s) for s in text.split(";\n")) < 100_000  # D1's statement limit
        for stmt in text.split(";\n"):
            if stmt.startswith('INSERT INTO "v_'):
                cols = stmt[: stmt.index(") VALUES")].count(",") + 1
                assert (stmt.count("),(") + 1) * cols <= d1.MAX_VALUES
        db.executescript(text)
    src = sqlite3.connect(
        out / "d" / "qld-road-crash-locations" / "v" / "2026-04-24" / "data.sqlite"
    )
    (tbl,) = db.execute(
        "SELECT tbl FROM _versions WHERE slug = 'qld-road-crash-locations'"
    ).fetchone()
    assert tbl.startswith(d1.table_name("qld-road-crash-locations", "2026-04-24") + "_")
    assert (
        db.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()
        == src.execute("SELECT COUNT(*) FROM records").fetchone()
    )
    # D1 holds the rows in the Parquet's order, which the register sorts for this dataset.
    assert (
        db.execute(f'SELECT * FROM "{tbl}" ORDER BY crash_ref_number LIMIT 3').fetchall()
        == src.execute("SELECT * FROM records ORDER BY crash_ref_number LIMIT 3").fetchall()
    )
    assert [r[0] for r in db.execute(f'SELECT crash_year FROM "{tbl}" ORDER BY rowid')] == sorted(
        r[0] for r in src.execute("SELECT crash_year FROM records")
    )
    slug, version, t, fields, rows, attribution, header = db.execute(
        "SELECT * FROM _versions WHERE slug = 'qld-road-crash-locations'"
    ).fetchone()
    assert (t, rows) == (tbl, 300) and "Transport and Main Roads" in attribution
    assert {f["name"] for f in json.loads(fields)} >= {
        "crash_ref_number",
        "loc_local_government_area",
    }
    prov = json.loads(header)
    assert prov["licence"]["id"] == "CC-BY-4.0"
    assert prov["publisher"]["name"] == "Department of Transport and Main Roads"
    assert prov["source"]["sha256"] and prov["source"]["fetched_at"] and prov["source"]["url"]
    indexes = {r[1] for r in db.execute(f"PRAGMA index_list('{tbl}')")}
    assert any("loc_local_government_area" in i for i in indexes)


def test_only_the_newest_versions_are_kept_once_the_new_one_is_registered(tmp_path):
    fake = FakeD1()
    for v in ("2026-01-01", "2026-04-01"):
        _deploy(fake, tmp_path / v, [("x", v, 3)])
    _deploy(fake, tmp_path / "c", [("x", "2026-07-01", 3)])
    reg = d1._registry(fake)
    assert sorted(v for _, v in reg) == ["2026-04-01", "2026-07-01"]
    tables = _tables(fake)
    assert not any("20260101" in t for t in tables)
    assert {r["tbl"] for r in reg.values()} <= tables
    assert fake.query("SELECT COUNT(*) AS n FROM _loads") == [{"n": 0}]


def test_openapi_lists_the_query_api_only_once_it_is_switched_on(tmp_path, monkeypatch):
    from publicdata import site
    from publicdata.__main__ import main

    monkeypatch.setattr(site, "QUERY_API", False)
    assert main(["build", "--fixtures", "--out", str(tmp_path / "off")]) == 0
    off = json.loads((tmp_path / "off" / "openapi.json").read_text())
    assert not any(p.startswith("/api/v1/datasets/") for p in off["paths"])
    monkeypatch.setattr(site, "QUERY_API", True)
    assert main(["build", "--fixtures", "--out", str(tmp_path / "on")]) == 0
    on = json.loads((tmp_path / "on" / "openapi.json").read_text())
    rows = on["paths"]["/api/v1/datasets/{slug}/rows"]["get"]
    names = {p["name"] for p in rows["parameters"]}
    assert {"slug", "select", "order", "limit", "format"} <= names
    assert "crash_severity" not in names and "/d/<slug>/openapi.json" in rows["description"]
    assert "qld-road-crash-locations" in rows["parameters"][0]["schema"]["enum"]
    assert not any("qld-road-crash-locations" in p for p in on["paths"])
    own = json.loads(
        (tmp_path / "on" / "d" / "qld-road-crash-locations" / "openapi.json").read_text()
    )
    own_rows = own["paths"]["/api/v1/datasets/qld-road-crash-locations/rows"]["get"]
    assert {"crash_severity", "loc_local_government_area"} <= {
        p["name"] for p in own_rows["parameters"]
    }
    assert "/api/v1/datasets/{slug}/aggregate" in on["paths"]
    dated = on["paths"]["/api/v1/datasets/{slug}/versions/{version}/rows"]["get"]
    assert [p["name"] for p in dated["parameters"][:2]] == ["slug", "version"]
    assert "/api/v1/datasets/{slug}/versions" in on["paths"]
    assert not any(
        p.startswith("/api/d/") or p in ("/api/votes", "/api/request") for p in on["paths"]
    )
    assert set(rows["responses"]["429"]["headers"]) == {"Retry-After", "RateLimit-Policy"}
    site_out = tmp_path / "on"
    agents = (site_out / "agents" / "index.html").read_text(encoding="utf-8")
    assert 'id="query-api"' in agents and "60 requests in 10 seconds" in agents
    assert "429" in (site_out / "llms.txt").read_text(encoding="utf-8")
    for page in site_out.rglob("*.html"):
        assert "no rate limits" not in page.read_text(encoding="utf-8").lower(), page


def test_a_large_version_loads_in_parts_that_register_it_only_at_the_end(
    tmp_path, monkeypatch, site_copy
):
    out = site_copy
    monkeypatch.setattr(d1, "PART_BYTES", 20_000)
    from publicdata.register import load

    from .conftest import ROOT

    ds = [d for d in load(ROOT / "register") if d.slug == "qld-road-crash-locations"]
    parts = d1.write_loads([out], ds, {}, tmp_path / "load")
    assert len(parts) > 3 and [p.name for p in parts] == sorted(p.name for p in parts)
    assert "DROP TABLE" in parts[0].read_text() and "INTO _versions" in parts[-1].read_text()
    assert all("INTO _versions" not in p.read_text() for p in parts[:-1])
    assert all("CREATE INDEX" not in p.read_text() for p in parts[:-1])
    last = parts[-1].read_text().strip().splitlines()
    assert last[-2].startswith("INSERT OR REPLACE INTO _versions")
    assert last[-1].startswith("INSERT OR REPLACE INTO _orders")
    manifest = json.loads(
        (tmp_path / "load" / "qld-road-crash-locations@2026-04-24.json").read_text()
    )
    assert manifest["parts"] == [p.name for p in parts] and manifest["rows"] == 300
    assert len(manifest["cum"]) == len(parts) - 1 and manifest["cum"][-1] == 300
    assert manifest["cum"] == sorted(manifest["cum"]) and manifest["indexes"] >= 1
    db = sqlite3.connect(":memory:")
    for p in parts[:-1]:
        db.executescript(p.read_text())
    db.execute(d1.REGISTRY)
    db.execute(d1.ORDERS)
    # Without its indexes the registration does not hold.
    stmts = parts[-1].read_text().splitlines()
    db.execute(next(s for s in stmts if s.startswith("INSERT OR REPLACE INTO _versions")))
    assert db.execute("SELECT COUNT(*) FROM _versions").fetchone() == (0,)
    db.executescript(parts[-1].read_text())
    assert db.execute("SELECT rows FROM _versions").fetchone() == (300,)


def test_every_part_carries_the_run_stamp(tmp_path, site_copy):
    out = site_copy
    from publicdata.register import load

    from .conftest import ROOT

    ds = [d for d in load(ROOT / "register") if d.slug == "qld-road-casualties"]
    parts = d1.write_loads([out], ds, {}, tmp_path / "load", stamp="36393705927")
    assert all(p.read_text().startswith("-- load 36393705927\n") for p in parts)


class FakeD1:
    """SQLite behind the two calls the loader makes. `plan` says what each file call does:
    "ok", "error-applied" (applies, then reports an error) or "error" (applies nothing)."""

    def __init__(self, plan=(), broken=()):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.plan = list(plan)
        self.broken = broken
        self.files = []

    def file(self, path):
        self.files.append(path.name)
        what = self.plan.pop(0) if self.plan else "ok"
        if path.name.startswith(tuple(self.broken)):
            what = "error"
        if what != "error":
            self.db.executescript(path.read_text())
        return what == "ok"

    def query(self, sql):
        try:
            cur = self.db.execute(sql)
        except sqlite3.OperationalError as e:
            raise RuntimeError(str(e)) from e
        cols = [c[0] for c in cur.description or []]
        self.db.commit()
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def parts_for(site, tmp_path, monkeypatch, slug="qld-road-crash-locations"):
    from publicdata.register import load

    from .conftest import ROOT

    ds = [d for d in load(ROOT / "register") if d.slug == slug]
    monkeypatch.setattr(d1, "PART_BYTES", 20_000)
    folder = tmp_path / "load"
    return folder, d1.write_loads([site], ds, {}, folder, stamp="run1")


def test_an_import_that_reports_an_error_after_applying_is_accepted_on_its_counts(
    fixture_site, tmp_path, monkeypatch
):
    folder, parts = parts_for(fixture_site, tmp_path, monkeypatch)
    fake = FakeD1(["ok", "error-applied"])
    assert d1.load(folder, fake, log=lambda *_: None, workers=1) == 0
    assert fake.files[: len(parts)] == [p.name for p in parts]  # no part ran twice
    assert d1.registered(fake) == {("qld-road-crash-locations", "2026-04-24"): 300}


def test_a_part_that_did_not_apply_runs_again_and_the_load_goes_on_from_there(
    fixture_site, tmp_path, monkeypatch
):
    folder, parts = parts_for(fixture_site, tmp_path, monkeypatch)
    fake = FakeD1(["ok", "error"])
    assert d1.load(folder, fake, log=lambda *_: None, workers=1) == 0
    names = [p.name for p in parts]
    assert fake.files[: len(parts) + 1] == [names[0], names[1], *names[1:]]
    assert parts[1].read_text().startswith("-- load run1 retry 1\n")
    assert not parts[0].read_text().startswith("-- load run1 retry")
    (tbl,) = (r["tbl"] for r in d1._registry(fake).values())
    assert d1.holds(fake, tbl, 300)


def test_a_version_that_never_loads_is_never_registered(fixture_site, tmp_path, monkeypatch):
    folder, parts = parts_for(fixture_site, tmp_path, monkeypatch)
    fake = FakeD1(["ok", "error", "error"])  # the second part never applies
    assert d1.load(folder, fake, log=lambda *_: None, workers=1) == 1
    assert d1.registered(fake) == {}
    (row,) = fake.query("SELECT * FROM _loads")
    assert (row["part"], row["rows"], row["attempts"]) == (1, _manifest(folder)["cum"][0], 1)


def test_one_version_that_cannot_load_does_not_stop_the_next(fixture_site, tmp_path, monkeypatch):
    folder, parts = parts_for(fixture_site, tmp_path, monkeypatch)
    _write_job(folder, "zz-other", "2026-01-01", 1)
    lines = []
    fake = FakeD1(broken=("qld-road-crash-locations@",))
    assert d1.load(folder, fake, log=lines.append) == 1
    assert ("zz-other", "2026-01-01") in d1.registered(fake)  # the next version still loaded
    assert any("qld-road-crash-locations@2026-04-24 failed" in x for x in lines)


def test_a_version_too_large_for_d1_is_files_only_everywhere(tmp_path, monkeypatch):
    from publicdata.__main__ import main
    from publicdata.register import load

    from .conftest import ROOT

    out = tmp_path / "dist"
    big = out / "d" / "qld-road-crash-locations"
    monkeypatch.setattr(d1, "MAX_CSV", 100_000)
    assert main(["build", "--fixtures", "--out", str(out)]) == 0
    assert (big / "v" / "2026-04-24" / "data.csv").stat().st_size > d1.MAX_CSV
    page = (big / "index.html").read_text(encoding="utf-8")
    assert 'id="console"' not in page and not (big / "fields.json").exists()
    assert not (big / "openapi.json").exists() and (big / "explore" / "index.html").exists()
    listed = json.loads((out / "mcp" / "resources.json").read_text())["resources"]
    assert "qld-road-crash-locations" not in {r["name"] for r in listed}
    assert "qld-road-casualties" in {r["name"] for r in listed}
    doc = json.loads((out / "openapi.json").read_text())
    enum = doc["paths"]["/api/v1/datasets/{slug}/rows"]["get"]["parameters"][0]["schema"]["enum"]
    assert "qld-road-crash-locations" not in enum and "qld-road-casualties" in enum
    from publicdata.gate import check

    assert check(out, ROOT / "register") == []
    ds = [d for d in load(ROOT / "register") if d.slug == "qld-road-crash-locations"]
    assert d1.write_loads([out], ds, {}, tmp_path / "load") == []


def test_query_false_keeps_a_dataset_out_of_the_loads(tmp_path, site_copy):
    import dataclasses

    from publicdata.register import load

    from .conftest import ROOT

    out = site_copy
    ds = [d for d in load(ROOT / "register") if d.slug == "qld-road-casualties"]
    assert d1.write_loads([out], ds, {}, tmp_path / "a")
    off = [dataclasses.replace(ds[0], query=False)]
    assert d1.write_loads([out], off, {}, tmp_path / "b") == []


def test_versions_import_one_part_at_a_time_with_each_version_in_order(tmp_path):
    import threading
    import time

    folder = tmp_path / "load"
    for k in range(6):
        _write_job(folder, f"s{k}", "2026-01-01", 3)
    order, live, peak, lock = {}, [0], [0], threading.Lock()

    class Slow(FakeD1):
        def file(self, path):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            order.setdefault(path.name.split(".part")[0], []).append(path.name)
            with lock:
                live[0] -= 1
            return super().file(path)

    fake = Slow()
    assert d1.load(folder, fake, log=lambda *_: None) == 0
    # D1 refuses a second import while one runs, so the parts never overlap.
    assert peak[0] == 1
    loads = {k: v for k, v in order.items() if ".part" in v[0]}
    assert all(v == sorted(v) and len(v) == 5 for v in loads.values()) and len(loads) == 6
    assert len(d1.registered(fake)) == 6


def test_a_version_is_checked_only_while_no_other_import_runs(tmp_path):
    import threading
    import time

    folder = tmp_path / "load"
    for k in range(6):
        _write_job(folder, f"s{k}", "2026-01-01", 2)
    importing, clashes, lock = [0], [], threading.Lock()

    class Slow(FakeD1):
        def file(self, path):
            with lock:
                importing[0] += 1
            time.sleep(0.02)
            with lock:
                importing[0] -= 1
            return super().file(path)

        def query(self, sql):
            time.sleep(0.005)
            with lock:
                clashes.append(importing[0])
            return super().query(sql)

    assert d1.load(folder, Slow(), log=lambda *_: None) == 0
    assert clashes and not any(clashes)


def test_a_loaded_version_whose_fields_changed_is_loaded_again(tmp_path):
    from types import SimpleNamespace

    root = tmp_path / "dist"
    v = root / "d" / "x-y" / "v" / "2026-01-02"
    v.mkdir(parents=True)
    (root / "latest.json").write_text(json.dumps({"x-y": "2026-01-02"}))
    import pyarrow as pa
    import pyarrow.parquet as pq

    t = pa.table({"a": pa.array([1], pa.int64()), "sal_2021_name": ["Kingaroy"]})
    pq.write_table(t.replace_schema_metadata({"publicdata": "{}"}), v / "data.parquet")
    # The loader sizes the version's data.csv from its dataset's data package.
    (root / "d" / "x-y" / "datapackage.json").write_text(
        json.dumps({"resources": [{"path": "/d/x-y/v/2026-01-02/data.csv", "bytes": 8192}]})
    )
    fields = (SimpleNamespace(name="a", type="integer"), SimpleNamespace(name="sal_2021_name", type="string"))  # fmt: skip
    ds = SimpleNamespace(slug="x-y", key=(), partition_by=(), query=True, fields=fields)
    loaded = {"x-y": ["2026-01-02"]}
    same = json.dumps(d1.built_fields(ds, v / "data.parquet"))
    assert (
        d1.write_loads([root], [ds], loaded, tmp_path / "a", "", {("x-y", "2026-01-02"): same})
        == []
    )
    old = json.dumps([{"name": "a", "type": "integer"}])
    parts = d1.write_loads([root], [ds], loaded, tmp_path / "b", "", {("x-y", "2026-01-02"): old})
    assert [p.name for p in parts] == ["x-y@2026-01-02.part001.sql", "x-y@2026-01-02.part002.sql"]
    # A registry read without fields, as before deploys asked for them, reloads nothing.
    assert d1.write_loads([root], [ds], loaded, tmp_path / "c") == []


def test_a_catalogue_row_larger_than_d1_holds_skips_the_index_without_failing(tmp_path):
    row = {f: "x" for f in d1.CATALOGUE_FIELDS} | {"summary": "x" * (d1.MAX_ROW + 1)}
    path = tmp_path / "catalogue.sqlite"
    d1.catalogue_sqlite(path, [row], "2026-10-03")
    assert d1.catalogue_loads(path, [], tmp_path / "out") == []


def _write_job(folder, slug, version, rows, index=("a",)):
    """A load of `rows` one-column rows: the table's creation in part 1, then a row per part,
    then the part that indexes and registers it. The table is named for the row count, so the
    same arguments give the same load in every deploy."""
    folder.mkdir(parents=True, exist_ok=True)
    tbl = d1.load_table(slug, version, rows)
    keep = (d1.PART_BYTES, d1.MAX_VALUES)
    d1.MAX_VALUES = 1
    try:
        stmts = list(
            d1._table_sql(
                slug,
                version,
                index,
                [("a", "INTEGER")],
                {"attribution": "x"},
                [{"name": "a", "type": "integer"}],
                iter([(i,) for i in range(rows)]),
                tbl,
            )
        )
        d1.PART_BYTES = len(stmts[0][1]) + len(stmts[1][1]) + 2
        return d1._write_load(folder, slug, version, tbl, stmts, "run", d1.KEEP)
    finally:
        d1.PART_BYTES, d1.MAX_VALUES = keep


def _deploy(fake, folder, specs, **kw):
    for slug, version, rows in specs:
        _write_job(folder, slug, version, rows)
    lines = []
    kw.setdefault("workers", 1)
    return d1.load(folder, fake, log=lines.append, **kw), lines


def _tables(fake):
    return {r["name"] for r in fake.query("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _manifest(folder):
    (m,) = folder.glob("*.json")
    return json.loads(m.read_text())


def _load_row(fake, slug):
    rows = fake.query(f"SELECT * FROM _loads WHERE slug = '{slug}'")
    return rows[0] if rows else None


def _served(fake, slug):
    """What the API answers from: the newest registered version and the rows its table holds."""
    r = fake.query(
        f"SELECT version, tbl, rows FROM _versions WHERE slug = '{slug}' ORDER BY version DESC"
    )
    if not r:
        return None
    n = fake.query(f'SELECT COUNT(*) AS n FROM "{r[0]["tbl"]}"')[0]["n"]
    return r[0]["version"], r[0]["rows"], n


def test_loading_an_older_version_never_drops_a_newer_one(tmp_path):
    fake = FakeD1()
    _deploy(fake, tmp_path / "d0", [("x", "2026-03-01", 3)])
    _deploy(fake, tmp_path / "d1", [("x", "2026-02-01", 3)])
    _deploy(fake, tmp_path / "d2", [("x", "2026-01-01", 3)])
    assert sorted(v for _, v in d1.registered(fake)) == ["2026-01-01", "2026-03-01"]
    assert _served(fake, "x") == ("2026-03-01", 3, 3)


def test_a_version_that_failed_in_three_deploys_waits_for_a_change_or_a_retry(tmp_path):
    fake = FakeD1(broken=("x@2026-01-01.part002",))
    for k in range(d1.MAX_FAILURES):
        assert _deploy(fake, tmp_path / f"d{k}", [("x", "2026-01-01", 2)])[0] == 1
    assert _load_row(fake, "x")["attempts"] == d1.MAX_FAILURES
    ran = len(fake.files)
    summary = tmp_path / "summary.md"
    failed, lines = _deploy(fake, tmp_path / "d3", [("x", "2026-01-01", 2)], summary=summary)
    assert failed == 0 and len(fake.files) == ran  # nothing ran, nothing failed the deploy
    assert any(x.startswith("::warning") and "d1_retry: x" in x for x in lines)
    assert "| x@2026-01-01 | skipped |" in summary.read_text()
    # A forced retry runs it again, and the failure still counts.
    assert _deploy(fake, tmp_path / "d4", [("x", "2026-01-01", 2)], retry={"x"})[0] == 1
    assert len(fake.files) > ran
    # A new version of the dataset is not held back by the old one's failures.
    fake.broken = ()
    assert _deploy(fake, tmp_path / "d5", [("x", "2026-02-01", 2)])[0] == 0
    assert _served(fake, "x") == ("2026-02-01", 2, 2) and _load_row(fake, "x") is None


def test_a_failed_load_resumes_at_the_part_that_failed(tmp_path):
    fake = FakeD1(broken=("x@2026-01-01.part004",))
    assert _deploy(fake, tmp_path / "d0", [("x", "2026-01-01", 5)])[0] == 1
    row = _load_row(fake, "x")
    assert (row["part"], row["rows"], row["attempts"]) == (3, 2, 1)
    assert d1.registered(fake) == {}
    fake.broken, fake.files = (), []
    failed, lines = _deploy(fake, tmp_path / "d1", [("x", "2026-01-01", 5)])
    assert failed == 0 and any("resumes after part 3" in x for x in lines)
    assert fake.files[0] == "x@2026-01-01.part004.sql"
    assert _served(fake, "x") == ("2026-01-01", 5, 5)
    (tbl,) = (r["tbl"] for r in d1._registry(fake).values())
    assert [r["a"] for r in fake.query(f'SELECT a FROM "{tbl}" ORDER BY rowid')] == list(range(5))
    assert _load_row(fake, "x") is None


def test_a_resume_whose_table_disagrees_with_its_progress_loads_from_part_one(tmp_path):
    fake = FakeD1(broken=("x@2026-01-01.part004",))
    assert _deploy(fake, tmp_path / "d0", [("x", "2026-01-01", 5)])[0] == 1
    tbl = _load_row(fake, "x")["tbl"]
    fake.db.execute(f'DELETE FROM "{tbl}" WHERE rowid = 1')
    fake.broken, fake.files = (), []
    failed, lines = _deploy(fake, tmp_path / "d1", [("x", "2026-01-01", 5)])
    assert failed == 0 and any("loading it from part 1" in x for x in lines)
    assert fake.files[0] == "x@2026-01-01.part001.sql"
    assert [r["a"] for r in fake.query(f'SELECT a FROM "{tbl}" ORDER BY rowid')] == list(range(5))


def test_a_version_rebuilt_between_deploys_loads_afresh_and_drops_the_part_filled_table(tmp_path):
    fake = FakeD1(broken=("x@2026-01-01.part004",))
    assert _deploy(fake, tmp_path / "d0", [("x", "2026-01-01", 5)])[0] == 1
    stale = _load_row(fake, "x")["tbl"]
    fake.broken, fake.files = (), []
    # Other rows for the same version: another table, so the old progress cannot apply.
    assert _deploy(fake, tmp_path / "d1", [("x", "2026-01-01", 6)])[0] == 0
    assert fake.files[0] == "x@2026-01-01.part001.sql"
    assert _served(fake, "x") == ("2026-01-01", 6, 6) and stale not in _tables(fake)


class Flaky(FakeD1):
    """D1 that stops answering queries on request."""

    down = False

    def query(self, sql):
        if self.down and "COUNT(*)" in sql:
            raise RuntimeError("D1 query failed: rate limited")
        return super().query(sql)


def test_a_check_d1_does_not_answer_is_never_read_as_not_loaded(tmp_path):
    fake = Flaky(broken=("x@2026-01-01.part004",))
    assert _deploy(fake, tmp_path / "d0", [("x", "2026-01-01", 5)])[0] == 1
    row = _load_row(fake, "x")
    assert d1.holds(fake, row["tbl"], 2) and not d1.holds(fake, "v_nowhere", 0)
    fake.down, fake.broken, fake.files = True, (), []
    with pytest.raises(d1.Unknown):
        d1.holds(fake, row["tbl"], 2)
    failed, lines = _deploy(fake, tmp_path / "d1", [("x", "2026-01-01", 5)])
    # Skipped for this deploy: nothing reloaded, the progress kept, no failure counted.
    assert failed == 0 and fake.files == [] and _load_row(fake, "x") == row
    assert any("::warning" in x and "unchecked" in x for x in lines)
    fake.down = False
    assert _deploy(fake, tmp_path / "d2", [("x", "2026-01-01", 5)])[0] == 0
    assert fake.files[0] == "x@2026-01-01.part004.sql"


def test_loads_past_the_budget_wait_and_the_longest_waiting_go_first(tmp_path):
    fake = FakeD1()
    # Each plans 2 rows written per row: the row and its one index entry.
    summary = tmp_path / "s.md"
    specs = [("a", "2026-01-01", 10), ("b", "2026-01-01", 10), ("c", "2026-01-01", 12)]
    _deploy(fake, tmp_path / "d0", specs, budget=45, now="2026-10-07T01:00:00", summary=summary)
    assert {s for s, _ in d1.registered(fake)} == {"a", "b"}
    assert _load_row(fake, "c")["since"] == "2026-10-07T01:00:00"
    assert "| c@2026-01-01 | deferred | 12 | 24 |" in summary.read_text()
    # c has waited longest, so it goes before a smaller newcomer.
    specs = [("c", "2026-01-01", 12), ("d", "2026-01-01", 1)]
    _deploy(fake, tmp_path / "d1", specs, budget=25, now="2026-10-07T02:00:00")
    assert {s for s, _ in d1.registered(fake)} == {"a", "b", "c"}
    assert _load_row(fake, "d")["since"] == "2026-10-07T02:00:00"
    _deploy(fake, tmp_path / "d2", [("d", "2026-01-01", 1)], budget=25)
    assert _load_row(fake, "d") is None and ("d", "2026-01-01") in d1.registered(fake)


def test_a_version_larger_than_the_budget_loads_alone_when_it_is_first_in_line(tmp_path):
    fake = FakeD1()
    specs = [("a", "2026-01-01", 30), ("b", "2026-01-01", 20), ("c", "2026-01-01", 10)]
    loaded = []
    for k in range(3):
        _deploy(fake, tmp_path / f"d{k}", specs, budget=5, now=f"2026-10-07T0{k}:00:00")
        loaded.append({s for s, _ in d1.registered(fake)})
    assert loaded == [{"c"}, {"b", "c"}, {"a", "b", "c"}]


def test_a_load_part_way_through_goes_before_the_rest(tmp_path):
    fake = FakeD1(broken=("z@2026-01-01.part003",))
    _deploy(fake, tmp_path / "d0", [("z", "2026-01-01", 20)], now="2026-10-07T03:00:00")
    fake.broken = ()
    specs = [("a", "2026-01-01", 1), ("z", "2026-01-01", 20)]
    _deploy(fake, tmp_path / "d1", specs, budget=5)
    assert {s for s, _ in d1.registered(fake)} == {"z"}


def test_the_previous_version_serves_until_the_next_one_is_whole(tmp_path):
    fake = FakeD1()
    _deploy(fake, tmp_path / "d0", [("x", "2026-01-01", 3)])
    # The new version waits behind a smaller load, and the old one answers meanwhile.
    _deploy(fake, tmp_path / "d1", [("a", "2026-01-01", 1), ("x", "2026-02-01", 50)], budget=5)
    assert _served(fake, "x") == ("2026-01-01", 3, 3)
    assert ("x", "2026-02-01") not in d1.registered(fake)
    # A failed load of it leaves the old one answering too.
    fake.broken = ("x@2026-02-01.part010",)
    assert _deploy(fake, tmp_path / "d2", [("x", "2026-02-01", 50)])[0] == 1
    assert _served(fake, "x") == ("2026-01-01", 3, 3)
    fake.broken = ()
    assert _deploy(fake, tmp_path / "d3", [("x", "2026-02-01", 50)])[0] == 0
    assert _served(fake, "x") == ("2026-02-01", 50, 50)
    assert {v for s, v in d1.registered(fake) if s == "x"} == {"2026-01-01", "2026-02-01"}


class Watch(FakeD1):
    """Checks after every statement D1 runs that each registered table is whole and indexed."""

    def check(self):
        for r in self.db.execute("SELECT tbl, rows FROM _versions").fetchall():
            assert self.db.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0] == r[1]
            idx = self.db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
                (r[0],),
            ).fetchone()[0]
            assert idx == 1, r

    def file(self, path):
        what = self.plan.pop(0) if self.plan else "ok"
        if path.name.startswith(tuple(self.broken)):
            what = "error"
        self.files.append(path.name)
        if what == "crash":
            raise OSError("the runner stopped")
        if what == "error":
            return False
        for stmt in path.read_text().split(";\n"):
            if stmt.strip() and not stmt.startswith("-- load") or "\n" in stmt.strip():
                self.db.executescript(stmt + ";")
                self.check()
        return what == "ok"


def test_no_registered_table_is_ever_partial_through_failures_crashes_and_a_reload(tmp_path):
    fake = Watch()
    fake.db.execute(d1.REGISTRY)
    _deploy(fake, tmp_path / "d0", [("x", "2026-01-01", 3)])
    first = d1._registry(fake)[("x", "2026-01-01")]["tbl"]
    # The same version again with other rows, as when a column is joined in: it fills a table of
    # its own while the registered one answers, through a crash and a failed finish.
    fake.plan = ["ok", "ok", "crash"]
    assert _deploy(fake, tmp_path / "d1", [("x", "2026-01-01", 5)])[0] == 1
    assert d1._registry(fake)[("x", "2026-01-01")]["tbl"] == first
    assert _load_row(fake, "x")["part"] == 0  # where a crash left it is not known
    fake.broken = ("x@2026-01-01.part007",)  # the finish
    assert _deploy(fake, tmp_path / "d2", [("x", "2026-01-01", 5)])[0] == 1
    assert _load_row(fake, "x")["part"] == 6
    assert d1._registry(fake)[("x", "2026-01-01")]["tbl"] == first
    fake.broken, fake.files = (), []
    assert _deploy(fake, tmp_path / "d3", [("x", "2026-01-01", 5)])[0] == 0
    assert fake.files == ["x@2026-01-01.part007.sql", "x@2026-01-01.clean.sql"]
    assert _served(fake, "x") == ("2026-01-01", 5, 5) and first not in _tables(fake)


def test_a_version_registered_by_a_deploy_that_could_not_confirm_it_only_finishes(tmp_path):
    fake = FakeD1()
    _deploy(fake, tmp_path / "d0", [("x", "2026-01-01", 3)])
    fake.files = []
    failed, _ = _deploy(fake, tmp_path / "d1", [("x", "2026-01-01", 3)])
    assert failed == 0 and fake.files == ["x@2026-01-01.part005.sql", "x@2026-01-01.clean.sql"]
    assert _served(fake, "x") == ("2026-01-01", 3, 3)


def test_the_load_plan_goes_to_the_step_summary(tmp_path):
    fake = FakeD1()
    summary = tmp_path / "summary.md"
    _deploy(fake, tmp_path / "d0", [("a", "2026-01-01", 2)], summary=summary)
    text = summary.read_text()
    assert "## D1 load" in text and "| a@2026-01-01 | loaded | 2 | 4 |" in text
