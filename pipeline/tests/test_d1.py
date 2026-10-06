import json
import sqlite3
import subprocess
import sys

from publicdata import d1


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
    tbl = d1.table_name("qld-road-crash-locations", "2026-04-24")
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


def test_only_the_newest_versions_are_kept():
    stmts = list(d1.prune_sql("x", ["2026-01-01", "2026-04-01"], "2026-07-01", keep=2))
    assert stmts == [
        'DROP TABLE IF EXISTS "v_x_20260101";',
        "DELETE FROM _versions WHERE slug = 'x' AND version = '2026-01-01';",
        "DELETE FROM _orders WHERE slug = 'x' AND version = '2026-01-01';",
    ]


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
    assert "DROP TABLE" in parts[0].read_text() and "_versions VALUES" in parts[-1].read_text()
    assert all("_versions VALUES" not in p.read_text() for p in parts[:-1])
    assert (
        parts[-1]
        .read_text()
        .strip()
        .splitlines()[-1]
        .startswith("INSERT OR REPLACE INTO _versions")
    )
    pruned = d1.write_loads(
        [out], ds, {"qld-road-crash-locations": ["2025-01-01", "2025-06-01"]}, tmp_path / "load2"
    )
    last = pruned[-1].read_text().strip().splitlines()
    assert last[-1].startswith("INSERT OR REPLACE INTO _versions") and any(
        "v_qld_road_crash_locations_20250101" in x for x in last
    )
    db = sqlite3.connect(":memory:")
    for p in parts:
        db.executescript(p.read_text())
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

    def __init__(self, plan=()):
        self.db = sqlite3.connect(":memory:")
        self.plan = list(plan)
        self.files = []

    def file(self, path):
        self.files.append(path.name)
        what = self.plan.pop(0) if self.plan else "ok"
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
    assert len(fake.files) == len(parts)  # verified first time, no retry
    assert d1.registered(fake) == {("qld-road-crash-locations", "2026-04-24"): 300}


def test_a_part_that_did_not_apply_reloads_the_version_from_its_first_part(
    fixture_site, tmp_path, monkeypatch
):
    folder, parts = parts_for(fixture_site, tmp_path, monkeypatch)
    fake = FakeD1(["ok", "error"])
    lines = []
    assert d1.load(folder, fake, log=lines.append, workers=1) == 0
    assert len(fake.files) == 2 * len(parts)
    assert all(p.read_text().startswith("-- load run1 retry 1\n") for p in parts)
    assert d1.holds(fake, "qld-road-crash-locations", "2026-04-24", 300)
    assert "attempt 2" in lines[-1] and "verified" in lines[-1]


def test_a_version_that_never_loads_is_unregistered(fixture_site, tmp_path, monkeypatch):
    folder, parts = parts_for(fixture_site, tmp_path, monkeypatch)
    first = ["ok", "error"] + ["ok"] * (len(parts) - 2)
    fake = FakeD1(first + first)  # the second part never applies, on either attempt
    assert d1.load(folder, fake, log=lambda *_: None, workers=1) == 1
    assert d1.registered(fake) == {}


def test_one_version_that_cannot_be_queried_does_not_stop_the_next(
    fixture_site, tmp_path, monkeypatch
):
    folder, parts = parts_for(fixture_site, tmp_path, monkeypatch)
    (folder / "zz-other@2026-01-01.part001.sql").write_text(
        d1.REGISTRY + '\nCREATE TABLE "v_zz_other_20260101" (x);\n'
        'INSERT INTO "v_zz_other_20260101" VALUES (1);\n'
        "INSERT OR REPLACE INTO _versions VALUES ('zz-other', '2026-01-01', 'v_zz_other_20260101', '[]', 1, '', '{}');\n"
    )

    class Flaky(FakeD1):
        def __init__(self):
            super().__init__()
            self.broken = True

        def file(self, path):
            if path.name.startswith("zz-"):
                self.broken = False
            return super().file(path)

        def query(self, sql):
            if self.broken:
                raise RuntimeError("D1 query failed: rate limited")
            return super().query(sql)

    lines = []
    fake = Flaky()
    assert d1.load(folder, fake, log=lines.append, workers=1) == 1
    assert ("zz-other", "2026-01-01") in d1.registered(fake)  # the next version still loaded
    assert any("qld-road-crash-locations@2026-04-24 failed" in x for x in lines)
    assert any("could not be unregistered" in x for x in lines)


def test_a_version_too_large_for_d1_is_files_only_everywhere(tmp_path, monkeypatch):
    from publicdata.__main__ import main
    from publicdata.register import load

    from .conftest import ROOT

    out = tmp_path / "dist"
    big = out / "d" / "qld-road-crash-locations"
    monkeypatch.setattr(d1, "MAX_SQLITE", 100_000)
    assert main(["build", "--fixtures", "--out", str(out)]) == 0
    assert (big / "v" / "2026-04-24" / "data.sqlite").stat().st_size > d1.MAX_SQLITE
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


def test_versions_import_one_part_at_a_time_with_each_version_in_order(tmp_path, monkeypatch):
    import threading
    import time

    folder = tmp_path / "load"
    folder.mkdir()
    for k in range(6):
        key = f"s{k}@2026-01-01"
        for part in (1, 2, 3):
            (folder / f"{key}.part{part:03d}.sql").write_text(f"-- {k} {part}\n")
    order, live, peak, lock = {}, [0], [0], threading.Lock()

    class Slow:
        def file(self, path):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            order.setdefault(path.name.split(".part")[0], []).append(path.name)
            with lock:
                live[0] -= 1
            return True

    monkeypatch.setattr(d1, "registered", lambda db: {(f"s{k}", "2026-01-01"): 1 for k in range(6)})
    monkeypatch.setattr(d1, "holds", lambda *a: True)
    assert d1.load(folder, Slow(), log=lambda *_: None) == 0
    # D1 refuses a second import while one runs, so the parts never overlap.
    assert peak[0] == 1
    assert all(v == sorted(v) and len(v) == 3 for v in order.values()) and len(order) == 6


def test_a_version_is_checked_only_while_no_other_import_runs(tmp_path, monkeypatch):
    import threading
    import time

    folder = tmp_path / "load"
    folder.mkdir()
    for k in range(6):
        for part in (1, 2):
            (folder / f"s{k}@2026-01-01.part{part:03d}.sql").write_text(f"-- {k} {part}\n")
    importing, clashes, lock = [0], [], threading.Lock()

    class Slow:
        def file(self, path):
            with lock:
                importing[0] += 1
            time.sleep(0.02)
            with lock:
                importing[0] -= 1
            return True

    def registered(db):
        time.sleep(0.01)
        with lock:
            clashes.append(importing[0])
        return {(f"s{k}", "2026-01-01"): 1 for k in range(6)}

    monkeypatch.setattr(d1, "registered", registered)
    monkeypatch.setattr(d1, "holds", lambda *a: True)
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
    # The loader sizes the version's data.sqlite from its dataset's data package.
    (root / "d" / "x-y" / "datapackage.json").write_text(
        json.dumps({"resources": [{"path": "/d/x-y/v/2026-01-02/data.sqlite", "bytes": 8192}]})
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
    assert [p.name for p in parts] == ["x-y@2026-01-02.part001.sql"]
    # A registry read without fields, as before deploys asked for them, reloads nothing.
    assert d1.write_loads([root], [ds], loaded, tmp_path / "c") == []


def test_a_catalogue_row_larger_than_d1_holds_skips_the_index_without_failing(tmp_path):
    row = {f: "x" for f in d1.CATALOGUE_FIELDS} | {"summary": "x" * (d1.MAX_ROW + 1)}
    path = tmp_path / "catalogue.sqlite"
    d1.catalogue_sqlite(path, [row], "2026-10-03")
    assert d1.catalogue_loads(path, [], tmp_path / "out") == []
