import json
import json as _json
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from publicdata import REPO, explorer, gate
from publicdata import __main__ as cli
from publicdata import api_text as at
from publicdata.__main__ import main
from publicdata.build import build_dataset
from publicdata.gate import check, examples
from publicdata.provenance import cite
from publicdata.register import Field, RegisterError, load, parse
from publicdata.site import (
    _change_words,
    _console,
    _temporal,
    download_name,
    linkify,
    register_path,
)
from publicdata.store import Manifest

from .conftest import ROOT, as_parquet, make_dataset, make_manifest, present

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from publicdata.register import Dataset


def test_full_fixture_build_passes_gate(  # noqa: PLR0915 - one fixture build, checked page by page
    register_dir: Path, tmp_path: Path, site_copy: Path
) -> None:
    out = site_copy
    assert check(out, register_dir) == []
    home = (out / "index.html").read_text(encoding="utf-8")
    assert "has not endorsed" in home or "No government agency" in home
    assert "static.cloudflareinsights.com/beacon.min.js" in home
    assert "googletagmanager" not in home
    assert '"token": "b3b3d9ce88104e7e965284919b4d556e"' in home  # the site token, not the site tag
    assert '<meta http-equiv="origin-trial" content="AjNME/' in home
    assert home.index("origin-trial") < home.index("<script")
    assert "<style>" in home
    assert 'rel="stylesheet"' not in home
    assert 'rel="preload" href="/static/fonts/RG-StandardRegular.woff2"' in home
    assert '"Random Grotesque";font-weight:300' in home
    assert ">all formats<" in home
    assert ">more<" not in home
    assert 'toolname="find_dataset_page"' in home
    # Every dataset link on the home page reaches a built page: the explorer is only built for
    # a table under its size cap, and the hero and preview must not point at one that is not.
    for href in set(re.findall(r'href="(/d/[^"#?]*)"', home)):
        target = out / href.lstrip("/")
        assert target.exists() or (target / "index.html").exists(), href
    # The showcase hides the rest of the cards on the home page only; a topic page shows all.
    assert "data-card hidden" in home
    for tpage in (out / "topics").glob("*/index.html"):
        assert "data-card hidden" not in tpage.read_text(encoding="utf-8"), tpage
    assert 'toolname="request_dataset"' in (out / "backlog" / "index.html").read_text(
        encoding="utf-8"
    )
    js = (out / "static" / "site.js").read_text(encoding="utf-8")
    for tool in (
        "search_datasets",
        "get_dataset",
        "list_fields",
        "list_partitions",
        "query_rows",
        "count_rows",
        "diff_versions",
        "list_backlog",
        "upvote_dataset",
    ):
        assert f"EXEC.{tool} = function" in js
        assert f'"{tool}": {{' in js
    assert not (out / "static" / "site.css").exists()
    assert "--primary-ink:#171717" in home
    assert "RG Fallback" in home
    assert "var API = '/api/v1/datasets/';" in js
    assert "partitionRows" not in js
    assert re.search(r'<script src="/static/site\.js\?v=[0-9a-f]{12}" defer>', home)
    ds = (out / "d" / "qld-road-crash-locations" / "index.html").read_text(encoding="utf-8")
    assert 'data-fmt="parquet"' in ds
    assert 'data-fmt="geojson"' in ds
    for key in ("xlsx", "gpkg", "geo.parquet", "csv.gz", "duckdb"):
        assert f'data-fmt="{key}"' in ds, key
    # Fetched after the size limits came in, so no Arrow; a version fetched before keeps it.
    assert 'data-fmt="arrow"' not in ds
    older = (out / "d" / "qld-road-casualties" / "index.html").read_text(encoding="utf-8")
    assert 'data-fmt="arrow"' in older
    # Every dataset page shows how to open it from Excel, Power BI, R, Python and DuckDB.
    assert "Use it in Excel, R, Python and more" in ds
    assert "latest/data.csv</code>" in ds
    assert (
        "await fetch(&#34;https://publicdata.au/api/v1/datasets/qld-road-crash-locations/aggregate?"
        in ds
    )
    # The home page's tool names link to what each tool does on the agents page.
    agents = (out / "agents" / "index.html").read_text(encoding="utf-8")
    for tool in ("search_datasets", "count_rows"):
        assert f'href="/agents/#tool-{tool}"' in home
        assert f'id="tool-{tool}"' in agents
    agents_md = (out / "agents" / "index.md").read_text(encoding="utf-8")
    assert "\n- `count_rows` Count and sum rows: Count, sum" in agents_md
    assert "pd_read(&#34;qld-road-crash-locations&#34;)" in ds
    assert (
        "ATTACH &#39;https://publicdata.au/d/qld-road-crash-locations/v/2026-04-24/data.duckdb&#39;"
        in ds
    )
    # A database: one DuckDB file and a Parquet per table, its licence condition on every page.
    db = (out / "d" / "gnaf" / "index.html").read_text(encoding="utf-8")
    assert (
        "<title>G-NAF download (Geocoded National Address File): DuckDB and Parquet | publicdata.au</title>"
        in db
    )
    assert "The licence has a condition" in db
    assert "sending of mail" in db
    assert "pd_connect(&#34;gnaf&#34;)" in db
    assert "pd_au.connect(" in db
    assert 'href="https://publicdata.au/d/gnaf/v/2026-08-17/tables/address_detail.parquet"' in db
    assert 'id="t-address_detail"' in db
    assert "FROM address_view" in db
    assert "data-fmt=" not in db
    assert 'id="console"' not in db
    assert "sending of mail" in (out / "d" / "gnaf" / "v" / "2026-08-17" / "index.html").read_text(
        encoding="utf-8"
    )
    dbmd = (out / "d" / "gnaf" / "index.md").read_text(encoding="utf-8")
    assert "kind: database" in dbmd
    assert "licence_condition: You must not" in dbmd
    assert "## Tables" in dbmd
    assert "- address_detail (40 rows, key address_detail_pid)" in dbmd
    assert "DuckDB https://publicdata.au/d/gnaf/v/2026-08-17/data.duckdb" in (
        out / "llms.txt"
    ).read_text(encoding="utf-8")
    catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    rec = next(d for d in catalog["dataset"] if d["identifier"] == "gnaf")
    assert rec["publicdata:kind"] == "database"
    assert rec["publicdata:licenceCondition"]
    assert rec["publicdata:jurisdiction"] == "Cth"
    assert rec["publicdata:topics"]
    places = json.loads((out / "places.json").read_text(encoding="utf-8"))
    lga = next(p for p in places["layers"] if p["key"] == "lga")
    assert lga["slug"] == "abs-lga-2025"
    assert lga["code"] == "lga_2025_code"
    assert lga["gpkg"].endswith(f"/d/abs-lga-2025/v/{lga['version']}/data.gpkg")
    assert rec["distribution"][0]["format"] == "duckdb"
    assert rec["distribution"][1]["title"] == "address_detail"
    assert not (out / "d" / "gnaf" / "explore").exists()
    assert not (out / "d" / "gnaf" / "openapi.json").exists()
    assert "/*.gpkg" in (out / "_headers").read_text(encoding="utf-8")
    assert "application/ld+json" in ds
    md = (out / "d" / "qld-road-crash-locations" / "index.md").read_text(encoding="utf-8")
    assert md.startswith("---\ntitle: Road crash locations, Queensland")
    assert (
        "<title>Queensland road crash locations 2001 to 2025: CSV, JSON, Parquet, GeoJSON download | publicdata.au</title>"
        in ds
    )
    # The collection owns the broad phrase, so it and its largest table never compete.
    assert "<title>Queensland road crash data 2001 to 2025: 6 tables" in (
        out / "c" / "qld-road-crashes" / "index.html"
    ).read_text(encoding="utf-8")
    # A visitor gets the office format first; Parquet stays a click away.
    assert ds.index('data-fmt="xlsx"') < ds.index('data-fmt="parquet"')
    assert (
        'id="url" tabindex="0">https://publicdata.au/d/qld-road-crash-locations/latest/data.xlsx'
        in ds
    )
    # The register's own questions are the caveats box, above the fold; the generated ones stay.
    assert '<div class="caveats">' in ds
    assert ds.index("caveats") < ds.index('id="get"')
    assert "Does it include every car accident in Queensland?" in ds.split('id="questions"')[0]
    assert "How do I download Road crash locations as a CSV file?" in ds.split('id="questions"')[1]
    assert '<table class="ledger sample">' in ds
    assert ds.count("<tr>", ds.index("ledger sample")) >= 11
    assert 'data-cite="harvard"' in ds
    assert "[data set], CC BY 4.0" in ds
    assert (out / "d" / "qld-road-crash-locations" / "schema.xlsx").stat().st_size > 1000
    assert 'href="https://publicdata.au/d/qld-road-crash-locations/schema.xlsx"' in ds
    # Place pages: one per council area, indexed, listed on the table's page and in the sitemap.
    assert '<h2 id="places">By council area</h2>' in ds
    place = out / "d" / "qld-road-crash-locations" / "in" / "gold-coast-city" / "index.html"
    ptext = place.read_text(encoding="utf-8")
    assert "<h1>Crashes in Gold Coast City</h1>" in ptext
    assert "noindex" not in ptext
    assert (
        place.with_name("index.md")
        .read_text(encoding="utf-8")
        .startswith('---\ntitle: "Crashes in Gold Coast City"')
    )
    # The place field is the By council area section, not a second list under Smaller files.
    assert "By loc_local_government_area" not in ds
    assert "By crash_year" in ds
    assert (
        'href="https://publicdata.au/d/qld-road-crash-locations/v/2026-04-24/by/loc_local_government_area/gold-coast-city.json"'
        in ptext
    )
    assert '"about": {"@id": "https://publicdata.au/d/qld-road-crash-locations/"}' in ptext
    assert (
        "/api/v1/datasets/qld-road-crash-locations/rows?loc_local_government_area=eq.Gold%20Coast%20City"
        in ptext
    )
    qld_map = (out / "sitemaps" / "qld.xml").read_text(encoding="utf-8")
    assert "https://publicdata.au/d/qld-road-crash-locations/in/gold-coast-city/" in qld_map
    # Version pages are kept and cited but not indexed, and stay out of the sitemap.
    vpage = (out / "d" / "qld-road-crash-locations" / "v" / "2026-04-24" / "index.html").read_text(
        encoding="utf-8"
    )
    assert '<meta name="robots" content="noindex, follow">' in vpage
    assert "/v/2026-04-24/" not in qld_map
    assert "https://publicdata.au/government/" in (out / "sitemaps" / "site.xml").read_text(
        encoding="utf-8"
    )
    gov = (out / "government" / "index.html").read_text(encoding="utf-8")
    assert "<h1" in gov
    assert "ABN 13 744 838 758" in gov
    # The header searches the whole catalogue; the phone header collapses the site links.
    assert 'class="search" action="/backlog/"' in home
    assert 'class="menu"' in home
    assert '<div class="ttile empty">' in home
    assert 'class="vote small"' in home
    assert "as at 30 June 2025" in home.split('class="ticker"')[1].split("</div>")[0]
    assert "<span>publishers</span>" in home
    assert "rows served" not in home
    assert 'href="/d/qld-road-crash-locations/"' in home
    pub = (out / "qld" / "transport-and-main-roads" / "index.html").read_text(encoding="utf-8")
    assert "Department of Transport and Main Roads is a Queensland government body" in pub
    assert "Its datasets served here cover roads and transport." in pub
    assert '"@type": "FAQPage"' in ds
    assert '"@type": "BreadcrumbList"' in ds
    assert '"citation": "Department of Transport and Main Roads' in ds
    assert 'id="cite-data"' in ds
    assert "@misc{publicdata_qld_road_crash_locations_2026_04_24" in ds
    assert 'href="https://publicdata.au/c/qld-road-crashes/"' in ds
    assert "## Questions" in md
    assert "Serialised and versioned by National Digital" in md
    assert "## By council area" in md
    assert "Harvard: Department of Transport" in md
    coll = (out / "c" / "qld-road-crashes" / "index.html").read_text(encoding="utf-8")
    assert "<h1>Crash data from Queensland roads</h1>" in coll
    assert coll.count('href="/d/qld-road-') >= 6
    assert coll.count('<p style="margin-top:14px">') == 2  # the two register paragraphs
    assert '<span class="chip live">live</span>' in coll
    assert ">www.data.qld.gov.au</a>" in coll
    aka = coll.split("Also called: ")[1].split("</p>")[0]
    assert "TMR crash data" in aka
    assert "road safety" not in aka
    assert "https://publicdata.au/c/qld-road-crashes/" in (out / "sitemaps" / "qld.xml").read_text(
        encoding="utf-8"
    )
    header = json.loads(
        (out / "d" / "qld-road-crash-locations" / "v" / "2026-04-24" / "data.ndjson")
        .open(encoding="utf-8")
        .readline()
    )["publicdata"]
    assert header["operator"]["name"] == "National Digital"
    assert header["cite"].endswith("/v/2026-04-24/")
    dp = json.loads(
        (out / "d" / "qld-road-crash-locations" / "datapackage.json").read_text(encoding="utf-8")
    )
    assert {
        "title": "National Digital",
        "path": "https://nationaldigital.com.au/",
        "role": "wrangler",
    } in dp["contributors"]
    for p in (
        ".well-known/ard.json",
        ".well-known/ai-catalog.json",
        "llms.txt",
        "catalog.json",
        "backlog.json",
    ):
        assert (out / p).exists()
    ard = (out / ".well-known/ard.json").read_text(encoding="utf-8")
    assert '"displayName"' in ard
    assert '"representativeQueries"' in ard
    headers = (out / "_headers").read_text(encoding="utf-8")
    assert "static.cloudflareinsights.com" in headers
    fn = (ROOT / "functions" / "d" / "[[path]].js").read_text(encoding="utf-8")
    assert "max-age=31536000, immutable" in fn
    assert "immutable, no-transform" in fn
    assert "max-age=300, no-transform" in fn
    assert "content-encoding" not in fn
    assert "obj.range.suffix !== undefined" in fn
    assert (
        "Download as CSV, Excel, JSON, GeoJSON, Parquet, SQLite, DuckDB, GeoPackage, GeoParquet, NDJSON, or"
        in ds
    )
    # Every HTML page names a Markdown twin that exists.
    for html in out.rglob("*.html"):
        if html.name == "404.html":
            continue
        m = re.search(
            r'type="text/markdown" href="https://publicdata.au/([^"]+)"',
            html.read_text(encoding="utf-8"),
        )
        assert m, html
        assert (out / m.group(1)).exists(), html


def test_the_gate_refuses_a_licence_that_is_not_open_or_differs_from_the_register(
    register_dir: Path, tmp_path: Path, site_copy: Path
) -> None:
    out = site_copy
    slug = "qld-road-crash-factors"
    (manifest,) = sorted((out / "d" / slug / "v").glob("*/manifest.json"))
    m = json.loads(manifest.read_text("utf-8"))
    m["licence"] = {**(m.get("licence") or {}), "id": "CC-BY-NC-4.0"}
    manifest.write_text(json.dumps(m), "utf-8")
    assert any(
        "portal licence 'CC-BY-NC-4.0' differs from register" in e for e in check(out, register_dir)
    )

    reg = tmp_path / "register"
    shutil.copytree(register_dir, reg)
    (entry,) = reg.rglob(f"{slug}.yaml")
    text = entry.read_text("utf-8")
    text = re.sub(r"(?m)^status: live$", "status: blocked\nblocked_reason: test", text)
    text = re.sub(r"(?m)^(  id: )CC-BY-4\.0$", r"\1CC-BY-NC-4.0", text, count=1)
    entry.write_text(text, "utf-8")
    errors = check(out, reg)
    assert any(f"{slug}: register licence CC-BY-NC-4.0 is not open" in e for e in errors)
    assert any(f"{slug}: status blocked but versions are published" in e for e in errors)


def test_home_links_to_the_dataset_when_its_table_has_no_explorer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No fixture Parquet fits under one byte, so no dataset gets an explorer page.
    monkeypatch.setattr(explorer, "MAX_PARQUET", 1)
    out = tmp_path / "dist"
    assert main(["build", "--fixtures", "--out", str(out)]) == 0
    home = (out / "index.html").read_text(encoding="utf-8")
    assert not list(out.glob("d/*/explore"))
    assert "/explore/" not in home
    # The fixtures hold no hero-map dataset, so the hero's own fallback shows only on a real
    # build; the explorer preview drops out here.
    assert ">Explore the map<" not in home
    assert ">Open the explorer<" not in home
    for href in set(re.findall(r'href="(/d/[^"#?]*)"', home)):
        target = out / href.lstrip("/")
        assert target.exists() or (target / "index.html").exists(), href


def test_temporal_coverage_is_only_what_the_publisher_states() -> None:
    m = make_manifest(b"x", as_at="2025-06-30")
    assert (
        _temporal(make_dataset([Field("a", "A")], temporal_start="2001-01-01"), m)
        == "2001-01-01/2025-06-30"
    )
    assert _temporal(make_dataset([Field("a", "A")]), m) is None
    assert (
        _temporal(
            make_dataset([Field("a", "A")], temporal_start="2001-01-01"),
            make_manifest(b"x", as_at=""),
        )
        is None
    )


def test_openapi_document_names_every_live_slug(
    register_dir: Path, tmp_path: Path, site_copy: Path
) -> None:
    out = site_copy
    doc = json.loads((out / "openapi.json").read_text(encoding="utf-8"))
    assert doc["openapi"] == "3.1.0"
    assert doc["servers"][0]["url"] == "https://publicdata.au"
    slugs = doc["paths"]["/d/{slug}/versions.json"]["get"]["parameters"][0]["schema"]["enum"]
    assert "qld-road-crash-locations" in slugs
    assert "/api/v1/votes/{slug}" in doc["paths"]
    assert "post" in doc["paths"]["/api/v1/votes/{slug}"]


def test_linkify_escapes_and_links_only_the_url() -> None:
    assert (
        linkify('See https://publicdata.au/d/x/ and <b>"quoted"</b>.')
        == 'See <a href="https://publicdata.au/d/x/">https://publicdata.au/d/x/</a> and &lt;b&gt;&quot;quoted&quot;&lt;/b&gt;.'
    )
    assert (
        linkify('https://a.example/p"x')
        == '<a href="https://a.example/p">https://a.example/p</a>&quot;x'
    )
    assert linkify("See https://x.gov.au/data?a=1&b=2 for details.") == (
        'See <a href="https://x.gov.au/data?a=1&amp;b=2">https://x.gov.au/data?a=1&amp;b=2</a> for details.'
    )
    assert linkify("Open https://a.example/data.csv, then stop.") == (
        'Open <a href="https://a.example/data.csv">https://a.example/data.csv</a>, then stop.'
    )


def test_harvard_is_author_date_with_the_version_and_no_access_date() -> None:
    ds = make_dataset([], title="Road crashes, Test")
    m = make_manifest(b"x", version="2026-04-24", fetched_at="2026-09-24T00:00:00+00:00")
    h = cite(ds, m, "https://publicdata.au/d/t/v/2026-04-24/")["harvard"]
    assert h.startswith("Test Agency (2026) Road crashes, Test, version 2026-04-24 [data set]")
    assert h.endswith("Available at: https://publicdata.au/d/t/v/2026-04-24/")
    assert "Accessed" not in h


def test_bibtex_protects_the_institutional_author_and_the_title() -> None:
    ds = {d.slug: d for d in load(ROOT / "register")}["qld-road-crash-locations"]
    m = Manifest.read(ROOT / "store" / ds.slug / "2026-04-24" / "manifest.json")
    bib = cite(ds, m, "https://publicdata.au/d/qld-road-crash-locations/v/2026-04-24/")["bibtex"]
    assert "  author = {{Department of Transport and Main Roads}}," in bib
    assert "  title = {{Road crash locations, Queensland}}," in bib
    assert "  year = {2026}," in bib
    assert bib.startswith("@misc{publicdata_qld_road_crash_locations_2026_04_24,")


def test_dataset_page_carries_a_query_console_and_its_openapi(
    tmp_path: Path, site_copy: Path
) -> None:
    out = site_copy

    def console(slug: str) -> tuple[str, dict[str, Any]]:
        page = (out / "d" / slug / "index.html").read_text(encoding="utf-8")
        data = present(re.search(r'id="ds-data">(.*?)</script>', page, re.DOTALL)).group(1)
        return page, json.loads(data)["console"]

    page, c = console("qld-road-casualties")
    block = present(re.search(r'id="ds-data">(.*?)</script>', page, re.DOTALL)).group(1)
    assert "<" not in block
    assert ">" not in block
    assert 'id="query"' in page
    assert 'href="/api/v1/datasets/qld-road-casualties/rows?' in page
    # The register's example is the console's first query, and the tile answers it.
    assert c["example"] == {
        "filters": [{"field": "casualty_severity", "op": "eq", "value": "Hospitalised"}],
        "group": ["casualty_road_user_type"],
        "metric": "sum.casualty_count",
    }
    assert "Casualties by road user where severity is Hospitalised: " in page
    year = next(f for f in c["fields"] if f["name"] == "crash_year")
    region = next(f for f in c["fields"] if f["name"] == "crash_police_region")
    assert region["values"] == sorted(region["values"])
    assert None not in region["values"]

    doc = json.loads((out / "d" / "qld-road-casualties" / "openapi.json").read_text("utf-8"))
    rows = doc["paths"]["/api/v1/datasets/qld-road-casualties/rows"]["get"]
    desc = {p["name"]: p.get("description", "") for p in rows["parameters"]}
    assert f"Values: {year['min']}, " in desc["crash_year"]
    assert doc["paths"]["/api/v1/datasets/{slug}/aggregate"]["get"]["parameters"][0]["schema"][
        "enum"
    ] == ["qld-road-casualties"]
    md = (out / "d" / "qld-road-casualties" / "index.md").read_text(encoding="utf-8")
    assert "## Query" in md
    assert "/api/v1/datasets/qld-road-casualties/aggregate?group=" in md


def test_every_dataset_gets_an_explorer_with_a_first_dashboard(  # noqa: PLR0915 - one check per explorer panel
    register_dir: Path, tmp_path: Path, site_copy: Path
) -> None:
    out = site_copy

    def ex(slug: str, page: str = "explore") -> tuple[str, dict[str, Any]]:
        text = (out / "d" / slug / page / "index.html").read_text(encoding="utf-8")
        block = present(re.search(r'id="ex-data">(.*?)</script>', text, re.DOTALL)).group(1)
        assert "<" not in block
        assert ">" not in block
        return text, json.loads(block)

    kinds = {d.slug: d.kind for d in load(register_dir)}
    live = [p.parent.name for p in sorted((out / "d").glob("*/index.html"))]
    assert live
    for slug in live:
        if kinds[slug] == "database":
            # A database has no explorer: its tables are queried in place.
            assert not (out / "d" / slug / "explore").exists()
            continue
        page, data = ex(slug)
        assert data["page"] == f"/d/{slug}/explore/"
        assert data["embed"] is False
        assert ex(slug, "embed")[1]["embed"] is True
        assert f'href="/d/{slug}/explore/"' in (out / "d" / slug / "index.html").read_text("utf-8")
        assert "noindex" in page
        assert "has not endorsed" in page
        assert (
            data["versions"][0]["parquet"]
            == f"/d/{slug}/v/{data['versions'][0]['version']}/data.parquet"
        )
        assert "rows" in data["defaults"]["panels"]
        listed = json.loads((out / "d" / slug / "explore" / "versions.json").read_text("utf-8"))
        assert listed["versions"] == [v["version"] for v in data["versions"]]
        assert "## Explore" in (out / "d" / slug / "index.md").read_text(encoding="utf-8")
        assert re.search(r'src="/static/explorer\.js\?v=[0-9a-f]{12}"', page)

    # A table of counts charts the sum of its count field; a table of crashes counts rows.
    _, cas = ex("qld-road-casualties")
    assert cas["defaults"]["panels"]["by-group"]["aggregates"] == {"Casualties": "sum"}
    assert cas["labels"]["crash_police_region"] == "Police region"
    assert ex("qld-road-crash-factors")[1]["yesno"][0] == "involving_drink_driving"
    _, loc = ex("qld-road-crash-locations")
    by = loc["defaults"]["panels"]["by-group"]
    assert by["expressions"] == {"Crashes": "1"}
    assert by["columns"] == ["Crashes"]
    # The first dashboard groups by the register example's field.
    assert by["title"] == "Crashes by nature of crash and severity"
    assert loc["defaults"]["masters"] == ["by-group"]
    years = loc["defaults"]["panels"]["over-time"]
    assert years["group_by"] == ["Year"]
    assert "Year" not in years["expressions"]
    assert loc["text"] == ["crash_year"]
    assert loc["labels"]["crash_year"] == "Year"
    panels = loc["defaults"]["panels"]
    assert by["split_by"] == ["Severity"]
    assert years["plugin"] == "Y Area"
    assert panels["heatmap"]["group_by"] == ["Year"]
    assert panels["map"]["plugin"] == "Map Scatter"
    assert panels["map"]["columns"][:2] == ["Longitude", "Latitude"]
    assert "map" not in cas["defaults"]["panels"]

    vendor = out / loc["vendor"].strip("/")
    assert re.fullmatch(r"/static/vendor/[0-9a-f]{12}/", loc["vendor"])
    assert (vendor / "LICENSES.txt").exists()
    for f in ("duckdb.js", "@duckdb/duckdb-wasm/dist/duckdb-eh.wasm.gz"):
        assert (vendor / f).stat().st_size > 100_000
    assert max(p.stat().st_size for p in vendor.rglob("*")) < 25 * 1024 * 1024

    headers = (out / "_headers").read_text(encoding="utf-8")
    site_csp = headers.split("\n")[2]
    assert "wasm-unsafe-eval" not in site_csp
    assert "blob:" not in site_csp
    embed = headers.split("/d/:slug/embed/*\n")[1].split("\n\n")[0]
    assert "! Content-Security-Policy" in embed
    assert "frame-ancestors *" in embed
    explore = headers.split("/d/:slug/explore/*\n")[1].split("\n\n")[0]
    assert "'wasm-unsafe-eval'" in explore
    assert "frame-ancestors 'none'" in explore
    assert "https://tile.openstreetmap.org" in explore
    assert "tile.openstreetmap" not in site_csp

    api = json.loads((out / "openapi.json").read_text(encoding="utf-8"))["paths"]
    body = api["/api/v1/views"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert all(p.get("description") for p in body["properties"].values())
    assert api["/api/v1/views/{id}"]["get"]["parameters"][0]["description"]

    assert check(out, register_dir) == []
    (out / "d" / "qld-road-casualties" / "explore" / "versions.json").unlink()
    assert any("explore/versions.json missing" in e for e in check(out, register_dir))
    (vendor / "duckdb.js").unlink()
    assert any("vendor lacks duckdb.js" in e for e in check(out, register_dir))


def test_a_first_dashboard_without_a_category_or_a_year_is_the_rows() -> None:
    console = {
        "fields": [{"name": "id", "type": "integer"}, {"name": "note", "type": "string"}],
        "example": {"filters": [], "group": [], "metric": "count"},
    }
    ws = explorer.defaults(make_dataset([], row_label=""), console)
    assert list(ws["panels"]) == ["rows"]
    assert ws["layout"] == {"type": "tab-layout", "tabs": ["rows"]}
    assert "masters" not in ws


def test_labels_are_words_unique_and_never_a_field_name(register_dir: Path) -> None:
    for ds in load(register_dir):
        shown = [f.display for f in ds.fields]
        assert len(set(shown)) == len(shown)
        assert not set(shown) & {f.name for f in ds.fields}
    assert Field(name="sa4_name_2021", source="X").display == "SA4 name 2021"

    raw = yaml.safe_load((register_dir / "qld-road-casualties.yaml").read_text(encoding="utf-8"))
    raw["fields"][1]["label"] = raw["fields"][0].get("label") or "Year"
    with pytest.raises(RegisterError, match="not unique"):
        parse(raw, "qld-road-casualties")
    raw["fields"][1]["label"] = raw["fields"][0]["name"]
    with pytest.raises(RegisterError, match="not unique"):
        parse(raw, "qld-road-casualties")


def test_every_operator_node_resolves_to_one_organisation(tmp_path: Path, site_copy: Path) -> None:
    out = site_copy
    ld = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)

    def nodes(v: object) -> Iterator[dict[str, Any]]:
        if isinstance(v, dict):
            yield v
            for x in v.values():
                yield from nodes(x)
        elif isinstance(v, list):
            for x in v:
                yield from nodes(x)

    seen = 0
    for page in out.rglob("*.html"):
        for block in ld.findall(page.read_text(encoding="utf-8")):
            for n in nodes(json.loads(block)):
                if n.get("name") == "National Digital":
                    seen += 1
                    assert n["@id"] == "https://nationaldigital.com.au/#organization", page
    assert seen > 0
    catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["publisher"]["@id"] == "https://nationaldigital.com.au/#organization"


def test_catalog_modified_moves_with_the_newest_release(tmp_path: Path) -> None:
    store = tmp_path / "store"
    shutil.copytree(ROOT / "pipeline" / "tests" / "fixtures" / "store", store)
    old = store / "qld-road-casualties" / "2026-04-24"
    new = old.parent / "2026-10-01"
    shutil.copytree(old, new)
    m = json.loads((new / "manifest.json").read_text(encoding="utf-8"))
    m.update(version="2026-10-01", fetched_at="2026-10-01T02:00:00+00:00")
    (new / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
    out = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "publicdata", "build", "--store", str(store), "--out", str(out)],
        check=True,
    )
    catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["modified"] == "2026-10-01"
    by_id = {d["identifier"]: d for d in catalog["dataset"]}
    assert by_id["qld-road-casualties"]["modified"] == "2026-10-01"


def test_the_gate_refuses_a_page_that_leaves_out_the_licence_condition(
    register_dir: Path, fixture_store: Path, tmp_path: Path
) -> None:
    out = tmp_path / "dist"
    out.mkdir()
    ds = {d.slug: d for d in load(register_dir)}["gnaf"]
    build_dataset(ds, fixture_store, out)
    page = out / "d" / "gnaf" / "index.html"
    page.write_text("<p>has not endorsed</p>", encoding="utf-8")
    assert "d/gnaf/index.html: the licence condition is not stated" in check(out, register_dir)
    page.write_text(f"<p>has not endorsed. {ds.licence.condition}</p>", encoding="utf-8")
    assert not [e for e in check(out, register_dir) if "licence condition" in e]
    # The build writes the condition into every record the gate reads; stripping it from any
    # one of them is refused.
    for rel in (
        "d/gnaf/index.md",
        "d/gnaf/datapackage.json",
        "d/gnaf/v/2026-08-17/index.html",
        "d/gnaf/v/2026-08-17/index.md",
        "catalog.json",
    ):
        f = out / rel
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(ds.licence.condition, encoding="utf-8")
        kept = f.read_text(encoding="utf-8")
        assert ds.licence.condition[:60] in kept
        f.write_text(kept.replace(ds.licence.condition, ""), encoding="utf-8")
        assert f"{rel}: the licence condition is not stated" in check(out, register_dir)
        f.write_text(kept, encoding="utf-8")


def test_the_copy_checks_read_past_a_publishers_own_values(tmp_path: Path) -> None:
    idx = tmp_path / "d" / "x" / "v" / "2026-01-01" / "by" / "class"
    idx.mkdir(parents=True)
    (idx / "index.json").write_text(
        _json.dumps({"partitions": [{"value": "Flooring — Restricted"}]})
    )
    page = tmp_path / "d" / "x" / "in" / "restricted" / "index.html"
    page.parent.mkdir(parents=True)
    text = "<h1>Flooring — Restricted</h1><p>Our words.</p>"
    left = gate._without_publisher_values(text, page, tmp_path, {})
    assert left == "<h1></h1><p>Our words.</p>"
    # This site's own copy is still read.
    left = gate._without_publisher_values(text + "<p>A page — ours.</p>", page, tmp_path, {})
    assert left == "<h1></h1><p>Our words.</p><p>A page — ours.</p>"


def _console_db(tmp_path: Path, rows: Iterable[tuple[object, ...]]) -> Path:
    db = tmp_path / "data.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE records (row_id INTEGER, year INTEGER, state_code TEXT, state_name TEXT,"
        " kind TEXT, n INTEGER)"
    )
    con.executemany("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()

    return as_parquet(db)


def _console_ds(**kw: object) -> Dataset:
    return make_dataset(
        [
            Field("row_id", "Row", type="integer"),
            Field("year", "Year", type="integer"),
            Field("state_code", "Code"),
            Field("state_name", "Name"),
            Field("kind", "Kind"),
            Field("n", "N", type="integer"),
        ],
        **kw,
    )


def test_the_picked_example_skips_a_group_the_filter_fixes_and_an_identifier(
    tmp_path: Path,
) -> None:
    rows = [
        (i, 2020 + i % 2, str(i % 2), ("NSW", "Vic")[i % 2], ("a", "b", "c")[i % 3], i)
        for i in range(60)
    ]
    db = _console_db(tmp_path, rows)
    ex = _console(_console_ds(key=("year", "state_code", "kind")), db)["example"]
    # year is filtered; state_name is one value under it, so the group moves on to kind, and
    # row_id is an identifier, so n is the sum.
    assert ex["group"] == ["kind"]
    assert ex["metric"] == "sum.n"
    assert ex["filters"][0]["field"] == "year"
    # A filter that matches fewer than twenty rows is dropped.
    (tmp_path / "small").mkdir()
    small = _console_db(tmp_path / "small", rows[:6])
    assert _console(_console_ds(), small)["example"]["filters"] == []


def test_the_register_example_resolves_newest_and_must_name_present_fields(tmp_path: Path) -> None:
    db = _console_db(tmp_path, [(i, 2020 + i % 3, "1", "NSW", "a", i) for i in range(9)])
    example = {
        "filters": ({"field": "year", "op": "eq", "value": "newest"},),
        "group": ("kind",),
        "metric": "sum.n",
        "label": "Things",
    }
    ex = _console(_console_ds(example=example), db)["example"]
    assert ex == {
        "filters": [{"field": "year", "op": "eq", "value": "2022"}],
        "group": ["kind"],
        "metric": "sum.n",
        "label": "Things",
    }
    gone = {**example, "group": ("missing",)}
    with pytest.raises(ValueError, match="names missing"):
        _console(_console_ds(example=gone), db)
    gone = {**example, "metric": "sum.missing"}
    with pytest.raises(ValueError, match="names missing"):
        _console(_console_ds(example=gone), db)


def test_the_gate_lists_every_example_and_fails_a_register_example_with_no_answer(
    tmp_path: Path,
) -> None:
    def page(slug: str, answer: str) -> None:
        d = tmp_path / "d" / slug
        d.mkdir(parents=True)
        tile = f"<p>{answer}</p>" if answer else ""
        (d / "index.html").write_text(
            '<script type="application/json" id="ds-data">{"console": {"example": {}}}</script>'
            '<a class="tile"><b>Filter and count from a URL</b><p class="mono small">'
            f"aggregate?group=a&amp;metric=count</p>{tile}</a>",
            encoding="utf-8",
        )

    page("chosen", "")
    page("picked", "Rows by a: 3 x.")
    chosen = make_dataset([], slug="chosen", example={"filters": (), "group": ("a",)})
    report, errors = examples(
        tmp_path, {"chosen": chosen, "picked": make_dataset([], slug="picked")}
    )
    assert report == [
        "register chosen: aggregate?group=a&metric=count -> (no groups)",
        "rules    picked: aggregate?group=a&metric=count -> Rows by a: 3 x.",
    ]
    assert errors == ["chosen: the register's example query answers no rows"]


def test_a_change_without_a_key_is_told_by_its_row_counts() -> None:
    assert _change_words(None) == ""
    assert _change_words({"rows_from": 12, "rows_to": 15, "note": "No key."}) == " (12 rows before)"
    keyed = {"rows_from": 2, "rows_to": 2, "added": 1, "removed": 1, "changed": 1}
    assert _change_words(keyed) == " (1 added, 1 removed, 1 changed)"


def test_a_withheld_source_is_left_out_listed_and_not_expected(
    register_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = "qld-road-crash-factors"
    reg = tmp_path / "register"
    shutil.copytree(register_dir, reg)
    (entry,) = reg.rglob(f"{slug}.yaml")
    text = entry.read_text("utf-8")
    entry.write_text(
        re.sub(
            r"(?m)^status: live$", "status: live\nsource_withheld: Its terms are unclear.", text
        ),
        "utf-8",
    )
    monkeypatch.setattr(cli, "REGISTER", reg)
    out = tmp_path / "dist"
    assert cli.main(["build", "--fixtures", "--out", str(out)]) == 0
    (vdir,) = (out / "d" / slug / "v").glob("*/")
    assert not list(vdir.glob("source.*"))
    m = json.loads((vdir / "manifest.json").read_text("utf-8"))
    assert m["source_withheld"] == "Its terms are unclear."
    assert json.loads((out / "withheld.json").read_text("utf-8")) == [
        f"/d/{slug}/v/{vdir.name}/source.csv"
    ]
    assert "Its terms are unclear." in (out / "d" / slug / "index.html").read_text("utf-8")
    package = json.loads((out / "d" / slug / "datapackage.json").read_text("utf-8"))
    assert "source" not in {r["name"] for r in package["resources"]}
    assert check(out, reg) == []


def test_pages_invite_contributions_and_link_the_repository(fixture_site: Path) -> None:
    out = fixture_site
    for rel in ("index.html", "about/index.html", "backlog/index.html", "terms/index.html"):
        h = (out / rel).read_text(encoding="utf-8")
        assert f'href="{REPO}"' in h, rel
        assert 'href="/contribute/"' in h, rel
    page = (out / "d" / "qld-road-crash-locations" / "index.html").read_text(encoding="utf-8")
    entry = present(re.search(rf'href="{REPO}/blob/main/([^"]+)"', page)).group(1)
    assert entry == "register/qld-road-crash-locations.yaml"
    assert (ROOT / entry).is_file()
    assert "template=data-problem.yml&amp;dataset=https%3A%2F%2Fpublicdata.au%2Fd%2F" in page
    assert entry in (out / "d" / "qld-road-crash-locations" / "index.md").read_text("utf-8")
    contribute = (out / "contribute" / "index.html").read_text(encoding="utf-8")
    assert 'id="add-a-dataset"' in contribute
    assert "{repo}" not in contribute
    assert f"{REPO}/blob/main/CONTRIBUTING.md#add-a-serialisation" in contribute

    ld = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
    home = [json.loads(b) for b in ld.findall((out / "index.html").read_text(encoding="utf-8"))]
    source = next(n for n in home if n["@type"] == "SoftwareSourceCode")
    catalog = next(n for n in home if n["@type"] == "DataCatalog")
    assert source["codeRepository"] == REPO
    assert "sameAs" not in catalog
    assert "https://github.com/National-Digital" in catalog["provider"]["sameAs"]

    assert (
        at.server_card()["repository"]
        == at.registry_server()["repository"]
        == {
            "url": REPO,
            "source": "github",
        }
    )
    assert f"{REPO}:" in (out / "llms.txt").read_text(encoding="utf-8")
    assert "https://publicdata.au/contribute/" in (out / "sitemaps" / "site.xml").read_text("utf-8")


def test_the_entry_link_keeps_the_folder_an_entry_sits_in() -> None:
    ds = make_dataset([], slug="x")
    assert register_path(ds) == "register/x.yaml"
    assert register_path(replace(ds, path="/w/register/qld/x.yaml")) == "register/qld/x.yaml"


def test_a_downloaded_file_is_named_after_its_dataset_and_version(fixture_site: Path) -> None:
    cases = json.loads((Path(__file__).parent / "fixtures" / "download_names.json").read_text())
    for c in cases:
        assert download_name(c["slug"], c["version"], c["rel"]) == c["name"], c["rel"]
    slug, version = "qld-road-crash-locations", "2026-04-24"
    page = (fixture_site / "d" / slug / "v" / version / "index.html").read_text(encoding="utf-8")
    assert f'download="{slug}_{version}.parquet"' in page
    assert f'download="{slug}_{version}_source.' in page
    ds = (fixture_site / "d" / slug / "index.html").read_text(encoding="utf-8")
    assert re.search(rf'id="dl" href="[^"]+" download="{slug}_{version}\.\w+"', ds)


def test_the_stable_url_guide_sits_under_the_publishers_page(fixture_site: Path) -> None:
    out = fixture_site
    page = (out / "publishers" / "stable-urls" / "index.html").read_text(encoding="utf-8")
    assert "versions.json" in page
    assert "schema_version" in page
    assert '<a href="/publishers/" aria-current="page">' in page
    assert "/publishers/stable-urls/" in (out / "publishers" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "/publishers/stable-urls/" in (out / "government" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "https://publicdata.au/publishers/stable-urls/" in (
        out / "sitemaps" / "site.xml"
    ).read_text(encoding="utf-8")
    assert "https://publicdata.au/publishers/stable-urls/index.md" in (out / "llms.txt").read_text(
        encoding="utf-8"
    )
    md = (out / "publishers" / "stable-urls" / "index.md").read_text(encoding="utf-8")
    assert md.startswith("---\ntitle: Publishing a dataset at a stable URL\n")
    assert "## A check list" in md


def test_a_version_page_shows_the_version_notes(fixture_site: Path) -> None:
    page = (
        fixture_site / "d" / "qld-road-crash-locations" / "v" / "2026-04-24" / "index.html"
    ).read_text(encoding="utf-8")
    assert "About this version" in page
    assert "fixture: first 300 rows of the release" in page
    md = (
        fixture_site / "d" / "qld-road-crash-locations" / "v" / "2026-04-24" / "index.md"
    ).read_text(encoding="utf-8")
    assert "## About this version" in md
    assert "fixture: first 300 rows of the release" in md
    assert "immutable: true" not in md


def test_no_page_promises_a_version_never_changes(fixture_site: Path) -> None:
    """A correction can rebuild a version, so no page or API document may say otherwise."""
    page = (
        fixture_site / "d" / "qld-road-crash-locations" / "v" / "2026-04-24" / "index.html"
    ).read_text(encoding="utf-8")
    assert "Immutable version" not in page
    for name in ("openapi.json", "llms.txt", "index.html"):
        text = (fixture_site / name).read_text(encoding="utf-8")
        assert "versions never change" not in text.lower(), name
        assert "immutable files" not in text, name
