import io
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import publicdata_au as pd_au

META = {
    "version": "2026-08-07",
    "attribution": "Publisher, CC BY 4.0.",
    "cite": "Cite.",
    "licence": {"id": "CC-BY-4.0"},
}


class Handler(BaseHTTPRequestHandler):
    hits: list = []
    throttle = 0
    failing = 0
    duckdb_bytes = b""
    gpkg_bytes = b""
    ranges: list = []

    def log_message(self, *a):
        pass

    def send(self, status, body, ctype="application/json", headers=()):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        Handler.hits.append((u.path, q, self.headers.get("User-Agent")))
        base = f"http://{self.headers['Host']}"
        if Handler.throttle:
            Handler.throttle -= 1
            return self.send(429, {"error": "slow down"}, headers=[("Retry-After", "0")])
        if Handler.failing:
            Handler.failing -= 1
            return self.send(503, {"error": "busy"})
        if u.path == "/api/v1/datasets":
            return self.send(200, {"results": [{"slug": "a", "q": q.get("q")}, {"slug": "b"}]})
        if u.path in ("/api/v1/datasets/a/rows", "/api/v1/datasets/a/versions/2026-08-07/rows"):
            off = int(q.get("offset", 0))
            data = [{"n": i} for i in range(5)]
            lim = int(q.get("limit", 100))
            page = data[off : off + lim]
            more = off + lim < len(data)
            nxt = (
                f"{base}{u.path}?{urllib.parse.urlencode({**q, 'offset': off + lim})}"
                if more
                else None
            )
            return self.send(
                200, {"publicdata": META, "version_page": "vp", "rows": page, "next": nxt}
            )
        if u.path == "/api/v1/datasets/a/aggregate":
            return self.send(
                200, {"publicdata": META, "rows": [{"g": 1, "count": 2}], "next": None}
            )
        if u.path == "/api/v1/datasets/nope/rows":
            return self.send(404, {"error": "No such dataset in the query API"})
        if u.path == "/d/a/versions.json":
            return self.send(200, {"latest": "2026-08-07", "versions": [{"version": "2026-08-07"}]})
        if u.path in ("/d/a/latest/schema.json", "/d/a/v/2026-08-07/schema.json"):
            return self.send(
                200, {"fields": [{"name": "n", "type": "integer"}], "primaryKey": ["n"]}
            )
        if u.path == "/d/db/v/2026-08-07/schema.json":
            return self.send(
                200,
                {
                    "kind": "database",
                    "tables": [{"name": "thing", "fields": [{"name": "id"}], "primaryKey": ["id"]}],
                    "views": [],
                },
            )
        if u.path == "/d/db/v/2026-08-07/tables/thing.parquet":
            import pyarrow as pa
            import pyarrow.parquet as pq

            buf = io.BytesIO()
            t = pa.table({"id": [7]}).replace_schema_metadata({"publicdata": json.dumps(META)})
            pq.write_table(t, buf)
            return self.send(200, buf.getvalue(), "application/vnd.apache.parquet")
        if u.path == "/d/a/v/2026-08-07/data.gpkg":
            return self.send(200, Handler.gpkg_bytes, "application/geopackage+sqlite3")
        if u.path == "/d/b/latest/data.gpkg":
            return self.send(404, {"error": "not here"})
        if u.path == "/d/a/v/2026-08-07/data.duckdb":
            return self.send(200, Handler.duckdb_bytes, "application/octet-stream")
        if u.path == "/d/a/datapackage.json":
            return self.send(
                200,
                {
                    "title": "A things",
                    "version": "2026-08-07",
                    "licenses": [{"name": "CC-BY-4.0", "title": "CC BY 4.0"}],
                    "contributors": [{"title": "Pub", "role": "publisher"}],
                    "publicdata:attribution": "Publisher.",
                },
            )
        if u.path == "/d/a/v/2026-08-01/manifest.json":
            return self.send(200, {"fetched_at": "2026-08-01T03:00:00+00:00"})
        if u.path == "/catalog.json":
            return self.send(
                200,
                {
                    "dataset": [
                        {
                            "identifier": "a",
                            "publisher": {"name": "Bureau of Things"},
                            "spatial": "Queensland",
                            "publicdata:jurisdiction": "Qld",
                            "publicdata:topics": ["roads"],
                        },
                        {
                            "identifier": "b",
                            "publisher": {"name": "Other"},
                            "spatial": "Commonwealth",
                            "publicdata:jurisdiction": "Cth",
                            "publicdata:topics": ["crime"],
                        },
                    ]
                },
            )
        if u.path == "/d/a/changes.json":
            step = {
                "from": "2026-08-01",
                "to": "2026-08-07",
                "added": 1,
                "url": f"{base}/d/a/diff/x.json",
            }
            return self.send(200, {"dataset": "a", "changes": [step]})
        if u.path == "/d/a/diff/x.json":
            return self.send(200, {"from": "2026-08-01", "to": "2026-08-07", "added_keys": [9]})
        if u.path == "/api/v1/datasets/t/versions/2026-01-01/rows":
            return self.send(
                200, {"publicdata": META, "rows": [{"day": "2026-01-02"}], "next": None}
            )
        if u.path == "/d/t/v/2026-01-01/schema.json":
            return self.send(
                200, {"fields": [{"name": "day", "type": "string", "description": "Old."}]}
            )
        if u.path == "/d/t/fields.json":
            return self.send(
                200,
                {
                    "fields": [
                        {
                            "name": "n",
                            "type": "integer",
                            "description": "A count.",
                            "min": 0,
                            "max": 9,
                        },
                        {"name": "day", "type": "date"},
                        {"name": "at", "type": "datetime"},
                        {"name": "flag", "type": "boolean"},
                        {"name": "g", "type": "string", "values": ["x", "y"]},
                    ]
                },
            )
        if u.path == "/api/v1/datasets/t/rows":
            rows = [
                {
                    "n": 1,
                    "day": "2026-01-02",
                    "at": "2026-01-02T10:00:00Z",
                    "flag": 1,
                    "g": "x",
                    "e": "e",
                },
                {"n": 2, "day": "not a date", "at": None, "flag": 0, "g": "y", "e": "f"},
            ]
            return self.send(200, {"publicdata": META, "rows": rows, "next": None})
        if u.path == "/d/a/v/2026-08-07/manifest.json":
            return self.send(200, {"dataset": "a", "version": "2026-08-07", "sha256": "abc"})
        if u.path == "/api/v1/catalogue":
            rows = [{"id": "qld-1", "title": "Water", "jur": "qld", "state": "votable"}]
            return self.send(200, {"total": 1, "next_offset": None, "rows": rows, "q": q})
        if u.path == "/places.json":
            return self.send(
                200,
                {
                    "layers": [
                        {
                            "key": "postcode",
                            "slug": "abs-postal-areas-2021",
                            "title": "Postal Area (2021)",
                            "code": "poa_2021_code",
                            "name": "poa_2021_name",
                            "noun": "postcode",
                            "version": "2021-06-24",
                        }
                    ]
                },
            )
        if u.path == "/api/v1/datasets/c/aggregate":
            meta = {**META, "licence": {"id": "X", "condition": "No mail lists."}}
            return self.send(200, {"publicdata": meta, "rows": [], "next": None})
        if u.path.startswith("/d/a/latest/"):
            self.send_response(302)
            self.send_header("Location", u.path.replace("/latest/", "/v/2026-08-07/"))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if u.path == "/d/a/v/2026-08-07/data.csv":
            return self.send(200, b"n\n1\n", "text/csv")
        if u.path == "/d/g/v/2026-08-07/schema.json":
            fields = [
                {"name": "n", "type": "integer"},
                {"name": "day", "type": "date"},
                {"name": "code", "type": "string"},
                {"name": "flag", "type": "boolean"},
                {"name": "x", "type": "number"},
            ]
            return self.send(200, {"fields": fields})
        if u.path == "/d/g/v/2026-08-07/data.csv.gz":
            import gzip

            body = (
                b"n,day,code,flag,x,suppressed\n1,2026-01-02,01234,true,0.1,\n,,,false,2.5,n;day\n"
            )
            return self.send(200, gzip.compress(body, mtime=0), "application/gzip")
        if u.path == "/d/g/v/2026-08-07/data.ndjson":
            Handler.ranges.append(self.headers.get("Range"))
            header = {**META, "not_endorsed": "The publisher has not endorsed this site."}
            body = json.dumps({"publicdata": header}).encode() + b"\n" + b'{"n":1}\n'
            return self.send(200, body, "application/x-ndjson")
        if u.path == "/d/a/v/2026-08-07/data.parquet":
            import pyarrow as pa
            import pyarrow.parquet as pq

            buf = io.BytesIO()
            header = {
                **META,
                "version": "2026-08-07",
                "not_endorsed": "The publisher has not endorsed this site.",
            }
            t = pa.table({"n": [1, 2, 3]}).replace_schema_metadata(
                {"publicdata": json.dumps(header)}
            )
            pq.write_table(t, buf)
            return self.send(200, buf.getvalue(), "application/vnd.apache.parquet")
        self.send(404, {"error": "not here"})


def last_api_hit():
    return next(h for h in reversed(Handler.hits) if h[0].startswith("/api/"))


@pytest.fixture
def client():
    Handler.hits = []
    Handler.throttle = 0
    Handler.failing = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield pd_au.Client(f"http://127.0.0.1:{srv.server_port}", retries=2)
    srv.shutdown()


def test_filters_render_in_the_apis_operator_form():
    assert str(pd_au.gte(2020)) == "gte.2020"
    assert str(pd_au.in_("QLD", "NSW")) == "in.(QLD,NSW)"
    assert str(pd_au.in_(["a", "b"])) == "in.(a,b)"
    assert str(pd_au.not_(pd_au.eq("x"))) == "not.eq.x"
    assert str(pd_au.is_null()) == "is.null"
    assert str(pd_au.ilike("*rider*")) == "ilike.*rider*"
    assert str(pd_au.eq(True)) == "eq.true"
    with pytest.raises(ValueError, match="comma"):
        pd_au.in_("a,b")


def test_rows_sends_where_select_and_order(client):
    r = client.rows(
        "a",
        {"state": "QLD", "year": pd_au.gte(2020), "lga": None, "sex": ["F", "M"]},
        select=["n", "m"],
        order="n.desc",
        limit=2,
    )
    path, q, ua = last_api_hit()
    assert path == "/api/v1/datasets/a/rows"
    assert q == {
        "state": "eq.QLD",
        "year": "gte.2020",
        "lga": "is.null",
        "sex": "in.(F,M)",
        "select": "n,m",
        "order": "n.desc",
        "limit": "2",
    }
    assert ua.startswith("publicdata-au-python/")
    assert r == [{"n": 0}, {"n": 1}] and r.page["next"]


def test_rows_carries_provenance(client):
    r = client.rows("a")
    assert (
        r.version == "2026-08-07" and r.attribution and r.cite and r.licence == {"id": "CC-BY-4.0"}
    )
    assert r.version_page == "vp"


def test_all_follows_every_page(client):
    r = client.rows("a", all=True, limit=2)
    assert [x["n"] for x in r] == [0, 1, 2, 3, 4] and r.page["next"] is None
    assert len([h for h in Handler.hits if h[0].endswith("/rows")]) == 3


def test_a_dated_version_goes_on_the_path(client):
    client.rows("a", version="2026-08-07")
    assert last_api_hit()[0] == "/api/v1/datasets/a/versions/2026-08-07/rows"
    with pytest.raises(ValueError, match="date"):
        client.rows("a", version="latest")


def test_aggregate(client):
    a = client.aggregate("a", group=["g", "h"], metric=["count", "sum.n"], where={"x": 1})
    assert last_api_hit()[1] == {"group": "g,h", "metric": "count,sum.n", "x": "eq.1"}
    assert a == [{"g": 1, "count": 2}] and a.version == "2026-08-07"


def test_429_waits_then_retries(client):
    Handler.throttle = 2
    assert client.datasets("crash")[0] == {"slug": "a", "q": "crash"}


def test_a_passing_server_error_is_retried(client, monkeypatch):
    monkeypatch.setattr(pd_au.time, "sleep", lambda s: None)
    Handler.failing = 2
    assert client.datasets("crash")[0]["slug"] == "a"
    Handler.failing = 5
    with pytest.raises(pd_au.PublicDataError) as err:
        client.datasets()
    assert err.value.status == 503


def test_429_past_the_retries_raises(client):
    Handler.throttle = 5
    with pytest.raises(pd_au.PublicDataError) as err:
        client.datasets()
    assert err.value.status == 429


def test_an_error_names_the_status_and_the_apis_message(client):
    with pytest.raises(pd_au.PublicDataError) as err:
        client.rows("nope")
    assert err.value.status == 404 and "No such dataset" in str(err.value)
    assert err.value.body == {"error": "No such dataset in the query API"}


def test_a_bad_slug_never_reaches_the_network(client):
    with pytest.raises(ValueError):
        client.rows("../etc")
    assert Handler.hits == []


def test_download_follows_latest_and_names_the_version(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = client.download("a", "csv")
    assert p.name == "a-2026-08-07.csv" and p.read_bytes() == b"n\n1\n"
    with pytest.raises(ValueError, match="format"):
        client.download("a", "docx")


def test_read_takes_provenance_from_the_file_it_read(client):
    pytest.importorskip("pandas")
    df = client.read("a", version="2026-08-07")
    assert list(df["n"]) == [1, 2, 3]
    assert df.attrs["publicdata"]["version"] == "2026-08-07"
    assert df.attrs["publicdata"]["attribution"] == "Publisher, CC BY 4.0."
    assert "has not endorsed" in df.attrs["publicdata"]["not_endorsed"]
    assert not any(h[0].endswith("datapackage.json") for h in Handler.hits)


def test_read_falls_back_to_the_gzipped_csv_without_pyarrow(client, monkeypatch):
    pd = pytest.importorskip("pandas")
    import builtins

    real = builtins.__import__

    def no_pyarrow(name, *a, **k):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_pyarrow)
    df = client.read("g", version="2026-08-07")
    assert list(df.columns) == ["n", "day", "code", "flag", "x", "suppressed"]
    assert str(df["n"].dtype) == "Int64" and df["n"].isna().tolist() == [False, True]
    assert df["day"].iloc[0] == pd.Timestamp("2026-01-02") and pd.isna(df["day"].iloc[1])
    assert df["code"].iloc[0] == "01234" and pd.isna(df["code"].iloc[1])
    assert df["flag"].tolist() == [True, False]
    assert df["x"].tolist() == [0.1, 2.5]
    assert df["suppressed"].tolist() == [[], ["n", "day"]]
    assert df.attrs["publicdata"]["attribution"] == "Publisher, CC BY 4.0."
    assert Handler.ranges[-1] == "bytes=0-65535"
    picked = client.read("g", version="2026-08-07", columns=["code", "n"])
    assert list(picked.columns) == ["code", "n"]
    with pytest.raises(ValueError, match="unknown fields"):
        client.read("g", version="2026-08-07", columns=["nope"])
    with pytest.raises(ImportError, match="only as Parquet"):
        client.read("db", "2026-08-07", table="thing")


def test_a_format_a_version_leaves_out_says_why(client, tmp_path):
    with pytest.raises(pd_au.PublicDataError, match="has no data.xlsx in this version") as err:
        client.download("a", "xlsx", "2026-08-07", tmp_path / "x.xlsx")
    assert err.value.status == 404 and "size limits" in str(err.value)
    with pytest.raises(pd_au.PublicDataError, match="location or a shape"):
        client.download("a", "geo.parquet", "2026-08-07", tmp_path / "x.geo.parquet")
    with pytest.raises(pd_au.PublicDataError, match="no caps field"):
        client.download("a", "arrow", "2026-08-07", tmp_path / "x.arrow")
    with pytest.raises(pd_au.PublicDataError, match="not here"):
        client.download("a", "csv.gz", "2026-08-07", tmp_path / "x.csv.gz")


def test_an_unreachable_site_raises_the_packages_own_error():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(pd_au.PublicDataError) as err:
        pd_au.Client(f"http://127.0.0.1:{port}", timeout=2).datasets()
    assert err.value.status == 0 and "could not reach" in str(err.value)


def test_versions_and_dataset(client):
    assert client.versions("a") == [{"version": "2026-08-07"}]
    assert client.dataset("a")["licenses"][0]["name"] == "CC-BY-4.0"


def test_site_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("PUBLICDATA_SITE", "http://example.test/")
    assert pd_au.Client().site == "http://example.test"


@pytest.mark.skipif(
    not os.environ.get("PUBLICDATA_LIVE"), reason="set PUBLICDATA_LIVE=1 to query the live site"
)
def test_live_site():
    slugs = [d["slug"] for d in pd_au.datasets()]
    assert "au-road-deaths" in slugs
    r = pd_au.rows("au-road-deaths", {"state": "QLD"}, limit=1)
    assert len(r) == 1 and r.attribution


def test_tables_of_a_table_and_of_a_database(client):
    assert client.tables("a") == [
        {"name": "records", "fields": [{"name": "n", "type": "integer"}], "primaryKey": ["n"]}
    ]
    assert [t["name"] for t in client.tables("db", "2026-08-07")] == ["thing"]


def test_a_table_of_a_database_is_read_as_parquet(client):
    pytest.importorskip("pyarrow")
    pytest.importorskip("pandas")
    df = client.read("db", "2026-08-07", table="thing")
    assert list(df["id"]) == [7] and df.attrs["publicdata"]["version"] == "2026-08-07"
    assert client.file_url("db", version="2026-08-07", table="thing").endswith(
        "/d/db/v/2026-08-07/tables/thing.parquet"
    )
    with pytest.raises(ValueError):
        client.file_url("db", "csv", "2026-08-07", table="thing")
    with pytest.raises(ValueError):
        client.file_url("db", table="../x")


def test_connect_attaches_the_newest_version_read_only(client, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    src = tmp_path / "data.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE records AS SELECT 1 AS n UNION ALL SELECT 2")
    con.execute("CREATE TABLE publicdata (key VARCHAR, value VARCHAR)")
    con.execute("INSERT INTO publicdata VALUES ('licence', '{\"id\": \"CC-BY-4.0\"}')")
    con.close()
    Handler.duckdb_bytes = src.read_bytes()
    con = client.connect("a")
    assert con.execute("SELECT count(*) FROM records").fetchone()[0] == 2
    assert con.publicdata["version"] == "2026-08-07" and con.publicdata["name"] == "a"
    assert con.publicdata["licence"] == {"id": "CC-BY-4.0"}
    assert any(h[0] == "/d/a/v/2026-08-07/data.duckdb" for h in Handler.hits)
    with pytest.raises(duckdb.Error):
        con.execute("INSERT INTO records VALUES (3)")
    con.close()
    with pytest.raises(ValueError, match="database name"):
        client.connect("a", name='x" (READ_ONLY); DROP TABLE records; --')


def test_datasets_filter_by_publisher_topic_and_jurisdiction(client):
    assert [d["slug"] for d in client.datasets(publisher="bureau")] == ["a"]
    assert [d["slug"] for d in client.datasets(topic="crime")] == ["b"]
    assert [d["slug"] for d in client.datasets(jurisdiction="queensland")] == ["a"]
    assert [d["slug"] for d in client.datasets(jurisdiction="Commonwealth")] == ["b"]
    assert client.datasets(topic="roads", jurisdiction="cth") == []
    with pytest.raises(ValueError, match="crime, roads"):
        client.datasets(topic="nope")
    with pytest.raises(ValueError, match="one piece of text"):
        client.datasets(publisher=["a"])


def test_changes_and_diff(client):
    assert client.changes("a")[0]["added"] == 1
    assert client.changes("a", since="2026-08-02") == []
    assert client.diff("a")["added_keys"] == [9]
    with pytest.raises(ValueError, match="first version"):
        client.diff("a", "2026-08-01")


def test_cite_as_text_and_bibtex(client):
    assert client.cite("a") == (
        "Pub (2026). A things. Version 2026-08-07, serialised and versioned by National Digital "
        f"at publicdata.au. Publisher. {client.site}/d/a/v/2026-08-07/"
    )
    bib = client.cite("a", format="bibtex")
    assert bib.startswith("@misc{a-2026-08-07,") and "author = {{Pub}}" in bib
    assert "read from the publisher on 2026-08-01" in client.cite("a", "2026-08-01")


def test_the_cache_keeps_a_version_and_reuses_it(client, tmp_path):
    client.cache = True
    client._cache_dir = tmp_path / "cache"
    client.read("a")
    client.read("a")
    assert sum(h[0] == "/d/a/v/2026-08-07/data.parquet" for h in Handler.hits) == 1
    [kept] = client.cache_list()
    assert (kept["dataset"], kept["version"], kept["file"]) == ("a", "2026-08-07", "data.parquet")
    out = client.download("a", path=tmp_path / "x.parquet")
    assert (
        out.read_bytes() == (client.cache_dir() / "a" / "2026-08-07" / "data.parquet").read_bytes()
    )
    assert client.cache_clear("a") == kept["bytes"]
    assert client.cache_list() == []
    with pytest.raises(ValueError, match="slug"):
        client.cache_clear(version="2026-08-07")
    with pytest.raises(ValueError, match="format"):
        client.download("a", "../../x", cache=True)
    assert not (tmp_path / "x").exists() and not (tmp_path / "cache" / "a").exists()


def test_nothing_is_kept_unless_asked(client, tmp_path):
    client._cache_dir = tmp_path / "cache"
    client.read("a")
    assert not (tmp_path / "cache").exists()


def test_a_licence_condition_is_shown_once(client):
    with pytest.warns(pd_au.LicenceCondition, match="No mail lists"):
        client.aggregate("c")
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client.aggregate("c")
        client.aggregate("a")


def test_relation_is_lazy_and_checks_the_table(client, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    src = tmp_path / "data.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE records AS SELECT range AS n FROM range(10)")
    con.close()
    Handler.duckdb_bytes = src.read_bytes()
    r = client.relation("a")
    assert r.filter("n >= 5").aggregate("count(*)").fetchone()[0] == 5
    client.relation("a")
    assert len(client._relations) == 1
    with pytest.raises(ValueError, match="no table or view"):
        client.relation("a", "nope")
    with pytest.raises(ValueError, match="version="):
        client.relation("a", "2026-08-07")


def test_read_geo_reads_the_layer_and_its_provenance(client, tmp_path):
    gpd = pytest.importorskip("geopandas")
    import sqlite3
    from contextlib import closing

    from shapely.geometry import Point

    src = tmp_path / "data.gpkg"
    gdf = gpd.GeoDataFrame({"id": [1, 2]}, geometry=[Point(153, -28), Point(151, -33)], crs=7844)
    gdf.to_file(src, layer="records", driver="GPKG")
    with closing(sqlite3.connect(src)) as db:
        db.execute("CREATE TABLE publicdata (key TEXT, value TEXT)")
        db.execute("""INSERT INTO publicdata VALUES ('licence', '{"id": "CC-BY-4.0"}')""")
        db.commit()
    Handler.gpkg_bytes = src.read_bytes()
    out = client.read_geo("a")
    assert len(out) == 2 and out.crs.to_epsg() == 7844
    assert out.attrs["publicdata"]["licence"] == {"id": "CC-BY-4.0"}
    with pytest.raises(pd_au.PublicDataError, match="no map layer"):
        client.read_geo("b")


def test_fields_and_typed_rows(client):
    f = client.fields("t")
    assert [x["name"] for x in f] == ["n", "day", "at", "flag", "g"]
    r = client.rows("t")
    assert r[0]["day"] == __import__("datetime").date(2026, 1, 2)
    assert r[0]["at"].hour == 10 and r[0]["at"].utcoffset().total_seconds() == 0
    assert r[0]["flag"] is True and r[1]["flag"] is False
    assert r[1]["day"] == "not a date" and r[1]["at"] is None and r[0]["e"] == "e"
    df = r.to_pandas()
    assert df.attrs["fields"] == {"n": "A count."}
    db = client.fields("db", "2026-08-07")
    assert db[0]["table"] == "thing"


def test_provenance_catalogue_and_file_url(client):
    assert client.provenance("a", "2026-08-07")["sha256"] == "abc"
    out = client.catalogue(
        "water", jurisdiction="Queensland", status=["votable", "served"], limit=5
    )
    assert out.total == 1 and out[0]["jurisdiction"] == "qld" and out[0]["status"] == "votable"
    q = last_api_hit()[1]
    assert q["jur"] == "qld" and q["state"] == "votable,served" and q["limit"] == "5"
    with pytest.raises(ValueError, match="jurisdiction is one of"):
        client.catalogue(jurisdiction="Narnia")
    assert client.file_url("a", "csv", "2026-08-07").endswith("/d/a/v/2026-08-07/data.csv")


def test_an_unreachable_site_is_its_own_error():
    c = pd_au.Client("http://127.0.0.1:9", retries=0, timeout=2)
    with pytest.raises(pd_au.SiteUnreachable):
        c.versions("a")


def test_close_releases_relation_connections(client, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    src = tmp_path / "data.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE records AS SELECT 1 AS n")
    con.close()
    Handler.duckdb_bytes = src.read_bytes()
    with client as c:
        c.relation("a")
        assert len(c._relations) == 1
    assert c._relations == {}


def test_read_takes_only_the_columns_asked_for(client):
    pytest.importorskip("pyarrow")
    df = client.read("a", columns=["n"])
    assert list(df.columns) == ["n"]
    with pytest.raises(ValueError, match="list of field names"):
        client.read("a", columns="n")


def test_codes_compare_as_text_with_leading_zeros():
    pytest.importorskip("pandas")
    assert pd_au._norm_codes([800, 4220, None, "16490 "], ["0800", "4220"]) == [
        "0800",
        "4220",
        None,
        "16490",
    ]


def test_join_boundaries_finds_the_layer_and_keeps_the_order(client, monkeypatch):
    gpd = pytest.importorskip("geopandas")
    import pandas as pd
    from shapely.geometry import box

    b = gpd.GeoDataFrame(
        {"poa_2021_code": ["0800", "4220"], "poa_2021_name": ["0800", "4220"]},
        geometry=[box(0, 0, 1, 1), box(5, 5, 6, 6)],
        crs=7844,
    )
    b.attrs["publicdata"] = {"attribution": "ABS"}
    monkeypatch.setattr(client, "read_geo", lambda slug, version=None, cache=None: b)
    df = pd.DataFrame({"poa_2021_code": ["4220", "9999", "0800"], "n": [1, 2, 3]})
    with pytest.warns(UserWarning, match="1 row"):
        g = client.join_boundaries(df)
    assert list(g["n"]) == [1, 2, 3] and g.crs.to_epsg() == 7844
    names = list(g["poa_2021_name"])
    assert names[0] == "4220" and pd.isna(names[1]) and names[2] == "0800"
    assert g.geometry.iloc[1] is None and g.attrs["boundaries"] == {"attribution": "ABS"}
    other = pd.DataFrame({"pc": [800]})
    assert list(client.join_boundaries(other, layer="postcode", by="pc")["poa_2021_name"]) == [
        "0800"
    ]
    with pytest.raises(ValueError, match="no column named for a boundary code"):
        client.join_boundaries(pd.DataFrame({"z": [1]}))
    with pytest.raises(ValueError, match="no boundary layer"):
        client.join_boundaries(df, layer="nowhere")


def test_a_pinned_version_is_typed_from_its_own_fields(client):
    r = client.rows("t", version="2026-01-01")
    assert r[0]["day"] == "2026-01-02"
    assert r.page["fields"] == {"day": "Old."}
    assert any(h[0] == "/d/t/v/2026-01-01/schema.json" for h in Handler.hits)
