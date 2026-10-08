import json
import re
import subprocess
import sys

import pytest

from publicdata.publishers import Publisher, clean_title, load_curated, resolve, slugify
from publicdata.register import RegisterError


def rec(portal, org, title, **kw):
    return {"portal": portal, "org": org, "org_title": title, **kw}


def test_organisations_resolve_to_publishers_by_curation_then_portal():
    tmr = Publisher(
        "transport-and-main-roads",
        "Department of Transport and Main Roads",
        "Qld",
        "state",
        short="TMR",
        orgs=["qld:transport-and-main-roads", "gov:tmr"],
        curated=True,
    )
    records = [
        rec("qld", "transport-and-main-roads", "Transport and Main Roads"),
        rec("gov", "tmr", "Department of Transport and Main Roads"),
        rec("gov", "city-of-hobart-open-data", "City of Hobart Open Data"),
        rec("nsw", "wollongong", "Wollongong City Council"),
        rec("vic", "melb", "City of Melbourne"),
        rec("vic", "melb-2", "City of Melbourne Open Data"),
    ]
    pubs, by_org = resolve(records, [tmr], {"qld": "qld", "gov": "cth", "nsw": "nsw", "vic": "vic"})
    assert by_org["gov:tmr"] is tmr and by_org["qld:transport-and-main-roads"] is tmr
    hobart = by_org["gov:city-of-hobart-open-data"]
    assert (hobart.jurisdiction, hobart.slug, hobart.name) == (
        "Cth",
        "city-of-hobart",
        "City of Hobart",
    )
    assert by_org["nsw:wollongong"].level == "local"
    assert by_org["vic:melb"] is by_org["vic:melb-2"]  # one council listed twice on one portal
    assert by_org["vic:melb"].path == "/vic/city-of-melbourne/"
    assert clean_title("City of Moreton Bay's Data Hub") == "City of Moreton Bay"
    assert slugify("Attorney-General’s Department") == "attorney-generals-department"


def test_curated_publishers_are_validated(tmp_path):
    (tmp_path / "qld.yaml").write_text(
        "- {slug: tmr, name: TMR, jurisdiction: Qld, orgs: [qld:tmr]}\n"
    )
    assert [p.path for p in load_curated(tmp_path)] == ["/qld/tmr/"]
    (tmp_path / "tas.yaml").write_text(
        "- {slug: hobart, name: Hobart, jurisdiction: Tas, orgs: [qld:tmr]}\n"
    )
    with pytest.raises(RegisterError, match="already under"):
        load_curated(tmp_path)
    (tmp_path / "tas.yaml").write_text("- {slug: Hobart City, name: Hobart, jurisdiction: Tas}\n")
    with pytest.raises(RegisterError, match="lower case"):
        load_curated(tmp_path)
    (tmp_path / "tas.yaml").write_text("- {slug: hobart, name: Hobart, jurisdiction: Tasmania}\n")
    with pytest.raises(RegisterError, match="jurisdiction"):
        load_curated(tmp_path)


# Open contributor issues: a backlog entry's, and a catalogue record's on the TMR page.
TASKS = {"abn-bulk-extract": 5, "qld-0a000000-0000-0000-0000-000000000002": 6}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site") / "dist"
    tasks = out.parent / "contribute.json"
    tasks.write_text(json.dumps(TASKS), encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "publicdata",
            "build",
            "--fixtures",
            "--out",
            str(out),
            "--search",
            str(out.parent / "search.sqlite"),
            "--tasks",
            str(tasks),
        ],
        check=True,
    )
    return out


def test_every_crumb_tier_is_a_page(site):
    ds = (site / "d" / "qld-road-casualties" / "index.html").read_text(encoding="utf-8")
    assert '<a href="/qld/">Queensland</a> / <a href="/qld/transport-and-main-roads/">' in ds
    crumbs = [
        json.loads(x)
        for x in __import__("re").findall(r'<script type="application/ld\+json">(.*?)</script>', ds)
    ]
    trail = next(c for c in crumbs if c.get("@type") == "BreadcrumbList")["itemListElement"]
    assert [i["name"] for i in trail][:3] == ["Datasets", "Queensland", "Transport and Main Roads"]
    for path in (
        "browse",
        "qld",
        "tas",
        "cth",
        "qld/transport-and-main-roads",
        "tas/city-of-hobart",
    ):
        assert (site / path / "index.html").exists() and (site / path / "index.md").exists()
    tmr = (site / "qld" / "transport-and-main-roads" / "index.html").read_text(encoding="utf-8")
    assert (
        "<title>Queensland Department of Transport and Main Roads (TMR) open data: 5 datasets"
        in tmr
    )
    assert 'href="/c/qld-road-crashes/">served<' in tmr  # the portal record that is served here
    assert 'data-vote="qld-0a000000-0000-0000-0000-000000000001"' in tmr
    assert "licence not open" in tmr and "no download" in tmr
    assert 'name="robots"' not in tmr


def test_thin_publisher_pages_stay_out_of_the_index(site):
    hobart = (site / "tas" / "city-of-hobart" / "index.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex, follow">' in hobart
    sitemap = "".join(p.read_text(encoding="utf-8") for p in (site / "sitemaps").glob("*.xml"))
    assert "/tas/city-of-hobart/" not in sitemap and "/qld/transport-and-main-roads/" in sitemap
    assert "/browse/" in sitemap and "/tas/" in sitemap


def test_only_votable_records_are_in_the_vote_shards(site):
    shards = {
        p.stem: json.loads(p.read_text()) for p in (site / "catalogue" / "votable").glob("*.json")
    }
    ids = {i for s in shards.values() for i in s}
    assert "qld-0a000000-0000-0000-0000-000000000001" in ids  # open, CSV
    assert "qld-0a000000-0000-0000-0000-000000000003" not in ids  # PDF only
    assert "qld-0a000000-0000-0000-0000-000000000004" not in ids  # ND licence
    assert "qld-f3e0ca94-2d7b-44ee-abef-d6b06e9b0729" not in ids  # served here already
    assert all(i.startswith(k) for k, s in shards.items() for i in s)
    listing = json.loads((site / "qld" / "transport-and-main-roads" / "catalogue.json").read_text())
    served = [r for r in listing["records"] if r["served_at"]]
    assert served and served[0]["served_at"].endswith("/c/qld-road-crashes/")


def test_other_organisations_and_unread_portals_are_shown_on_government_pages(site):
    cth = (site / "cth" / "index.md").read_text(encoding="utf-8")
    assert "## Universities, research bodies and other organisations" in cth
    assert "[James Cook University]" in cth
    act = (site / "act" / "index.html").read_text(encoding="utf-8")
    assert "data.act.gov.au catalogue could not be read on 28 September 2026" in act
    assert "as they stood on 21 September 2026" in act
    assert "could not be read" not in (site / "qld" / "index.html").read_text(encoding="utf-8")
    assert "data.act.gov.au catalogue could not be read" in (
        site / "browse" / "index.html"
    ).read_text(encoding="utf-8")


def test_before_the_first_harvest_the_directory_claims_nothing_about_the_portals(
    tmp_path, fixture_store
):
    store = tmp_path / "store"
    store.mkdir()
    for d in fixture_store.iterdir():
        if d.name != "catalogue":
            (store / d.name).symlink_to(d)
    out = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "publicdata", "build", "--store", str(store), "--out", str(out)],
        check=True,
    )
    browse = (out / "browse" / "index.html").read_text(encoding="utf-8")
    assert "is being gathered" in browse and "0 datasets" not in browse
    assert '<meta name="robots" content="noindex, follow">' in browse
    assert "Datasets listed" not in (out / "qld" / "index.html").read_text(encoding="utf-8")
    assert not any("/browse/" in p.read_text("utf-8") for p in (out / "sitemaps").glob("*.xml"))
    tmr = (out / "qld" / "transport-and-main-roads" / "index.md").read_text(encoding="utf-8")
    assert "## On the portals" not in tmr and "catalogue.json" not in tmr.split("---", 2)[2]


def test_template_copy_has_no_stray_escapes(site):
    for page in ("qld/index.html", "browse/index.html", "qld/transport-and-main-roads/index.html"):
        assert "\\'" not in (site / page).read_text(encoding="utf-8")


def test_a_pasted_url_is_read_the_way_functions_catalogue_reads_it():
    from pathlib import Path

    from publicdata.directory import locate

    cases = json.loads((Path(__file__).parent / "fixtures" / "locate.json").read_text())
    for c in cases:
        hit = locate(c["url"])
        assert (list(hit) if hit else None) == c["expect"], c["url"]


def test_a_live_entry_marks_the_record_its_source_url_names_as_served():
    from publicdata.directory import plan, search_rows
    from publicdata.register import load

    reg = {d.slug: d for d in load(__import__("publicdata.__main__").__main__.REGISTER)}
    rec = {
        "kind": "dataflow",
        "licence": "CC-BY-4.0",
        "open": True,
        "downloadable": True,
        "formats": ["API", "CSV", "SDMX"],
        "modified": "",
        "summary": "",
        "org": "abs",
        "org_title": "Australian Bureau of Statistics",
        "portal": "abs",
        "id": "abs-births-summary",
        "name": "BIRTHS_SUMMARY",
        "title": "Births, summary, by state",
        "url": "https://dataexplorer.abs.gov.au/vis?df[id]=BIRTHS_SUMMARY",
    }
    d = plan([reg["au-births-by-state"]], [rec], [], "2026-10-05")
    assert d.served == {"abs-births-summary": "/c/au-abs-births/"}
    assert search_rows(d)[0]["state"] == "served"


def test_a_planned_register_entry_takes_the_votes_of_its_catalogue_record():
    from publicdata.directory import plan, render, search_rows
    from publicdata.register import load

    reg = {d.slug: d for d in load(__import__("publicdata.__main__").__main__.REGISTER)}
    base = {
        "kind": "dataset",
        "licence": "CC-BY-4.0",
        "open": True,
        "downloadable": True,
        "formats": ["CSV"],
        "modified": "2026-09-01",
        "summary": "",
        "org": "abr",
        "org_title": "Australian Business Register",
    }
    records = [
        {
            **base,
            "id": "gov-1",
            "portal": "gov",
            "name": "abn-bulk-extract",
            "title": "ABN Bulk Extract",
            "url": "https://data.gov.au/data/dataset/abn-bulk-extract",
        },
        {
            **base,
            "id": "gov-2",
            "portal": "gov",
            "name": "other",
            "title": "Other",
            "url": "https://data.gov.au/data/dataset/other",
        },
        {
            **base,
            "id": "gov-3",
            "portal": "gov",
            "name": "closed",
            "title": "Closed",
            "url": "https://data.gov.au/data/dataset/closed",
            "open": False,
        },
    ]
    d = plan([reg["abn-bulk-extract"]], records, [], "2026-09-28")
    assert d.chosen == {"gov-1": "abn-bulk-extract"}
    rows = {r["id"]: r for r in search_rows(d)}
    assert (rows["gov-1"]["state"], rows["gov-1"]["vote"]) == ("chosen", "abn-bulk-extract")
    assert (rows["gov-2"]["state"], rows["gov-2"]["vote"]) == ("votable", "gov-2")
    assert (rows["gov-3"]["state"], rows["gov-3"]["note"]) == ("closed", "licence not open")
    assert rows["gov-1"]["jur"] == "cth" and rows["gov-1"]["host"] == "data.gov.au"
    assert d.votable == 2
    written = {}
    render(
        d, lambda *a, **k: None, lambda rel, text: written.__setitem__(rel, text), {}, lambda c: {}
    )
    shards = {
        k
        for rel, t in written.items()
        if rel.startswith("catalogue/votable/")
        for k in json.loads(t)
    }
    assert "gov-2" in shards and "gov-1" not in shards
    # Votes cast under the record id before the register claimed it count for the entry.
    assert json.loads(written["catalogue/aliases.json"]) == {"gov-1": "abn-bulk-extract"}
    listing = next(
        json.loads(t)
        for rel, t in written.items()
        if rel.endswith("catalogue.json") and "gov-1" in t
    )
    votes = {r["id"]: r["vote"] for r in listing["records"]}
    assert votes == {"gov-1": "abn-bulk-extract", "gov-2": "gov-2", "gov-3": None}


def test_the_search_index_holds_every_listed_record_and_loads_once_per_harvest(site, tmp_path):
    import sqlite3

    from publicdata.d1 import catalogue_loads

    path = site.parent / "search.sqlite"
    db = sqlite3.connect(path)
    states = dict(db.execute("SELECT state, COUNT(*) FROM records GROUP BY state").fetchall())
    (version,) = db.execute("SELECT value FROM publicdata WHERE key = 'catalogue_read'").fetchone()
    db.close()
    assert states["served"] >= 1 and states["votable"] >= 1 and states["closed"] >= 1
    parts = catalogue_loads(path, ["2020-01-01"], tmp_path, "run1")
    sql = "".join(p.read_text() for p in parts)
    tbl = json.loads((tmp_path / f"_catalogue@{version}.json").read_text())["tbl"]
    assert tbl.startswith(f"v__catalogue_{version.replace('-', '')}_")
    assert f'CREATE VIRTUAL TABLE "{tbl}_fts" USING fts5' in sql
    # The older index is dropped by the loader, only once this one is registered.
    assert "20200101" not in sql
    mem = sqlite3.connect(":memory:")
    for p in parts:
        mem.executescript(p.read_text())
    (rows,) = mem.execute("SELECT rows FROM _versions WHERE tbl = ?", (tbl,)).fetchone()
    assert rows == sum(states.values())
    assert catalogue_loads(path, [version], tmp_path / "again") == []
    home = (site / "backlog" / "index.html").read_text(encoding="utf-8")
    assert 'id="cat-search"' in home and '<option value="qld">Queensland</option>' in home


def test_the_served_index_loads_when_the_register_changes_and_answers_a_search(site, tmp_path):
    import sqlite3

    from publicdata.d1 import SERVED, served_loads

    path = site.parent / "search.sqlite"
    src = sqlite3.connect(path)
    (version,) = src.execute("SELECT value FROM publicdata WHERE key = 'served_version'").fetchone()
    slugs = {r[0] for r in src.execute("SELECT slug FROM served")}
    src.close()
    assert "qld-road-casualties" in slugs
    parts = served_loads(path, ["0000"], tmp_path, "run1")
    assert parts and served_loads(path, [version], tmp_path / "again") == []
    db = sqlite3.connect(":memory:")
    for p in parts:
        db.executescript(p.read_text())
    tbl = db.execute("SELECT tbl FROM _versions WHERE slug = ?", (SERVED,)).fetchone()[0]
    hit = db.execute(
        f'SELECT c.slug, c.latest FROM "{tbl}_fts" f JOIN "{tbl}" c ON c.rowid = f.rowid '
        f'WHERE "{tbl}_fts" MATCH \'"casualty"\' ORDER BY bm25("{tbl}_fts")'
    ).fetchall()
    assert hit[0] == (
        "qld-road-casualties",
        "https://publicdata.au/d/qld-road-casualties/latest/data.parquet",
    )


def test_the_sitemap_is_an_index_of_one_sitemap_per_government(site):
    index = (site / "sitemap.xml").read_text(encoding="utf-8")
    assert "<sitemapindex" in index and "https://publicdata.au/sitemaps/site.xml" in index
    qld = (site / "sitemaps" / "qld.xml").read_text(encoding="utf-8")
    assert "/d/qld-road-casualties/" in qld and "/c/qld-road-crashes/" in qld
    assert "/d/qld-road-casualties/" not in (site / "sitemaps" / "site.xml").read_text("utf-8")


def _ld(page, kind):
    html = page.read_text("utf-8")
    nodes = [
        json.loads(b)
        for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    ]
    return next(n for n in nodes if n.get("@type") == kind)


def test_a_dataset_with_copies_names_them_in_its_json_ld_and_on_the_page(site):
    page = site / "d" / "qld-road-crash-factors" / "index.html"
    node = _ld(page, "Dataset")
    assert node["identifier"][1] == {
        "@type": "PropertyValue",
        "propertyID": "DOI",
        "value": "10.5281/zenodo.1000001",
        "url": "https://doi.org/10.5281/zenodo.1000001",
    }
    assert node["sameAs"][1:] == [
        "https://huggingface.co/datasets/National-Digital/qld-road-crash-factors",
        "https://www.kaggle.com/datasets/NationalDigitalAU/qld-road-crash-factors",
        "https://doi.org/10.5281/zenodo.1000001",
    ]
    html = page.read_text("utf-8")
    for url in node["sameAs"][1:]:
        assert f'href="{url}"' in html


def test_a_dataset_without_copies_keeps_its_plain_identifier(site):
    node = _ld(site / "d" / "qld-road-casualties" / "index.html", "Dataset")
    assert node["identifier"] == "qld-road-casualties" and isinstance(node["sameAs"], str)
    assert 'id="copies"' not in (site / "d" / "qld-road-casualties" / "index.html").read_text(
        "utf-8"
    )


def test_the_operator_names_its_hub_accounts_on_the_home_page(site):
    provider = _ld(site / "index.html", "DataCatalog")["provider"]
    assert provider["sameAs"] == [
        "https://github.com/National-Digital",
        "https://huggingface.co/National-Digital",
        "https://www.kaggle.com/NationalDigitalAU",
    ]


def test_a_dataset_with_an_open_contributor_issue_links_it(site):
    issue = "https://github.com/National-Digital/publicdata.au/issues/"
    backlog = (site / "backlog" / "index.html").read_text(encoding="utf-8")
    assert (
        f'Open as a contributor task: <a href="{issue}5" rel="noopener">GitHub issue 5</a>'
        in backlog
    )
    assert backlog.count(issue) == 1
    tmr = (site / "qld" / "transport-and-main-roads" / "index.html").read_text(encoding="utf-8")
    assert f'<a href="{issue}6" rel="noopener">GitHub issue 6</a>' in tmr
    assert tmr.count(issue) == 1


def test_a_record_claimed_by_an_entry_shows_the_issue_under_either_key():
    from publicdata.directory import Directory

    d = Directory(pubs={}, ds_pub={}, chosen={"gov-1": "abn-bulk-extract"})
    d.tasks = {"gov-1": 4}
    assert d.task_url("abn-bulk-extract").endswith("/issues/4")
    assert d.task_url("gov-1").endswith("/issues/4")
    d.tasks = {"abn-bulk-extract": 7}
    assert d.task_url("gov-1").endswith("/issues/7")
    assert d.task_url("gov-2") == ""
