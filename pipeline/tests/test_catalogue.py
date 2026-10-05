import json
from pathlib import Path

from publicdata import catalogue, store


class Resp:
    def __init__(self, body):
        self.body = body
        self.status_code = 200

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeSession:
    """Answers the three CKAN actions, the Socrata catalogue and the ABS dataflow list."""

    def __init__(self, packages):
        self.packages = packages
        self.headers = {}

    def get(self, url, params=None, timeout=None, headers=None):
        params = params or {}
        if url.endswith("/license_list"):
            return Resp(
                {"result": [{"id": "cc-by", "title": "Creative Commons Attribution 3.0 Australia"}]}
            )
        if url.endswith("/organization_list"):
            orgs = [{"name": "tmr", "title": " Transport and Main Roads "}]
            return Resp({"result": orgs if params["offset"] == 0 else []})
        if url.endswith("/package_search"):
            assert "extras_original_harvest_source" in params["fl"]
            assert "dataset_type" in params["fl"]
            assert ",url" in params["fl"]
            rows = self.packages[params["start"] : params["start"] + params["rows"]]
            return Resp({"result": {"count": len(self.packages), "results": rows}})
        if "socrata" in url:
            res = [
                {
                    "resource": {
                        "id": "426s-vdu4",
                        "name": "Speed cameras",
                        "type": "dataset",
                        "attribution": "Access Canberra",
                        "createdAt": "2016-08-23T01:38:15Z",
                        "updatedAt": "2026-08-21T05:35:43Z",
                        "description": "Camera sites.",
                    },
                    "metadata": {"license": "Creative Commons Attribution 4.0 International"},
                    "permalink": "https://www.data.act.gov.au/d/426s-vdu4",
                }
            ]
            return Resp({"resultSetSize": 1, "results": [] if params["scroll_id"] else res})
        if "abs.gov.au" in url:
            return Resp(
                {
                    "data": {
                        "dataflows": [
                            {"id": "ABS_CENSUS_G01", "version": "1.0", "name": "Census G01"}
                        ]
                    }
                }
            )
        raise AssertionError(url)


def pkg(i, **kw):
    return {
        "id": f"0000000{i}-aaaa-bbbb-cccc-dddddddddddd",
        "name": f"crash-data-{i}",
        "title": f"Crash data {i}",
        "organization": "tmr",
        "license_id": "cc-by",
        "metadata_created": "2019-01-01T00:00:00",
        "metadata_modified": "2026-04-24T01:02:03",
        "res_format": ["csv", "Excel (.xlsx)", "SHP, TAB, KMZ"],
        "num_resources": 3,
        "dataset_type": "dataset",
        "notes": "**Crash** data. See [the guide](https://example.org).",
        **kw,
    }


def test_licence_titles_map_onto_register_ids():
    assert catalogue.licence_id("Creative Commons Attribution 3.0 Australia") == "CC-BY-3.0-AU"
    assert catalogue.licence_id("Creative Commons Attribution 4.0 International") == "CC-BY-4.0"
    assert catalogue.licence_id("Creative Commons Attribution 2.5 Australia") == "CC-BY-2.5-AU"
    assert (
        catalogue.licence_id("Creative Commons Non-Commercial 3.0 Australia") == "CC-BY-NC-3.0-AU"
    )
    assert catalogue.licence_id("CC-BY-ND-4.0") == "CC-BY-ND-4.0"
    assert catalogue.licence_id("Other (Open)") == "other-open"
    assert catalogue.licence_id("Other (Not Open)") == "other-closed"
    assert catalogue.is_open("other-closed") is False
    assert catalogue.licence_id("Other (Non-Commercial)") == "other-nc"
    assert catalogue.is_open("other-nc") is False
    assert catalogue.licence_id("Open Data Commons Attribution License") == "ODC-BY-1.0"
    assert catalogue.is_open("ODC-BY-1.0") is True
    assert catalogue.licence_id("Access on request") == "Access on request"
    assert catalogue.is_open("Access on request") is False
    assert catalogue.licence_id("Closed licence, Creative Commons excluded") != "CC-BY"
    assert catalogue.licence_id("CC-BY-SA-4.0") == "CC-BY-SA-4.0"
    assert catalogue.licence_id("Attribution 3.0 Australia (CC BY 3.0 AU)") == "CC-BY-3.0-AU"
    # Titles as the portals state them, 28 Sep 2026.
    assert catalogue.licence_id("Creative Commons Attribution Share-Alike 4.0") == "CC-BY-SA-4.0"
    assert (
        catalogue.licence_id("Creative Commons Attribution | Share Alike 4.0 International")
        == "CC-BY-SA-4.0"
    )
    assert (
        catalogue.licence_id(
            "Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International"
        )
        == "CC-BY-NC-ND-4.0"
    )
    assert catalogue.licence_id("Creative Commons Attribution No-Derivatives 4.0") == "CC-BY-ND-4.0"
    assert catalogue.licence_id("ODBL-1") == "ODBL-1.0" and catalogue.is_open("ODBL-1.0")
    assert (
        catalogue.licence_id(
            "Restricted access. This dataset is not available for public distribution."
        )
        != "CC-BY"
    )
    assert catalogue.is_open(catalogue.licence_id("Custom (Active Acceptance)")) is False
    assert catalogue.licence_id("Creative Commons Attribution") == "CC-BY"
    assert catalogue.licence_id("Licence Specified by Agency") == "Licence Specified by Agency"
    assert catalogue.licence_id("notspecified") == ""
    assert catalogue.is_open("CC-BY-3.0-AU") is True
    assert catalogue.is_open("CC-BY-NC-3.0-AU") is False
    assert catalogue.is_open("") is None


def test_formats_and_summary_are_tidied_not_rewritten():
    assert catalogue.formats(["csv", "Excel (.xlsx)", "SHP, TAB, KMZ", ".json"]) == [
        "CSV", "JSON", "KMZ", "SHP", "TAB", "XLSX",
    ]  # fmt: skip
    assert (
        catalogue.summary("**Crash** data. See [the guide](https://x).")
        == "Crash data. See the guide."
    )
    long = "word " * 100
    s = catalogue.summary(long)
    assert s.endswith("…") and len(s) <= catalogue.SUMMARY_CHARS + 1


def test_ckan_drops_copies_of_portals_read_directly():
    # data.gov.au returns the origin under this key, as a JSON string, when asked for
    # extras_original_harvest_source (checked against the live API on 28 Sep 2026).
    copy = pkg(2, original_harvest_source=json.dumps({"site_url": "https://data.nsw.gov.au/data/"}))
    science = pkg(
        3, original_harvest_source=json.dumps({"site_url": "https://catalogue.aodn.org.au"})
    )
    prefixed = pkg(
        4, extras_original_harvest_source=json.dumps({"site_url": "https://www.data.qld.gov.au/"})
    )
    # The shape abn-bulk-extract carries: data.gov.au names itself, so the record is its own.
    own = pkg(
        5,
        original_harvest_source=json.dumps(
            {
                "href": "https://data.gov.au/data/dataset/abn-bulk-extract",
                "site_url": "https://data.gov.au/data/dataset/",
                "title": "data.gov.au",
            }
        ),
    )
    s = FakeSession([pkg(1), copy, science, prefixed, own])
    recs, dropped = catalogue.ckan(catalogue.BY_CODE["gov"], s, log=lambda *_: None)
    assert dropped == 2
    assert [r["id"] for r in recs] == [
        "gov-00000001-aaaa-bbbb-cccc-dddddddddddd",
        "gov-00000003-aaaa-bbbb-cccc-dddddddddddd",
        "gov-00000005-aaaa-bbbb-cccc-dddddddddddd",
    ]
    assert recs[2]["harvested_from"] == ""
    r = recs[0]
    assert r["org_title"] == "Transport and Main Roads"
    assert r["licence"] == "CC-BY-3.0-AU" and r["open"] is True and r["downloadable"] is True
    assert r["url"] == "https://data.gov.au/data/dataset/crash-data-1"
    assert r["modified"] == "2026-04-24" and r["harvested_from"] == ""
    assert recs[1]["harvested_from"] == "catalogue.aodn.org.au"


def test_socrata_and_sdmx_records_fit_the_same_shape_and_vote_keys():
    s = FakeSession([])
    act, _ = catalogue.socrata(catalogue.BY_CODE["act"], s, log=lambda *_: None)
    abs_, _ = catalogue.sdmx(catalogue.BY_CODE["abs"], s, log=lambda *_: None)
    assert act[0]["id"] == "act-426s-vdu4" and act[0]["org_title"] == "Access Canberra"
    assert act[0]["licence"] == "CC-BY-4.0" and act[0]["modified"] == "2026-08-21"
    assert abs_[0]["id"] == "abs-abs-census-g01"
    assert set(act[0]) == set(abs_[0])
    import re

    assert all(re.match(r"^[a-z0-9][a-z0-9-]{1,63}$", r["id"]) for r in act + abs_)


def test_sdmx_dataflow_url_points_at_the_live_abs_data_explorer():
    s = FakeSession([])
    abs_, _ = catalogue.sdmx(catalogue.BY_CODE["abs"], s, log=lambda *_: None)
    # ABS serves the Data Explorer from dataexplorer.abs.gov.au; the old
    # explore.data.abs.gov.au host no longer resolves (see issue #26).
    assert (
        abs_[0]["url"] == "https://dataexplorer.abs.gov.au/vis?df[ds]=ABS_ABS_TOPICS"
        "&df[id]=ABS_CENSUS_G01&df[ag]=ABS&df[vs]=1.0"
    )
    assert "explore.data.abs.gov.au" not in abs_[0]["url"]


def test_snapshot_is_deterministic_and_unchanged_bytes_make_no_version(tmp_path, monkeypatch):
    s = FakeSession([pkg(1), pkg(2)])
    monkeypatch.setattr(catalogue.requests, "Session", lambda: s)
    portals = (catalogue.BY_CODE["qld"],)
    m = catalogue.fetch(tmp_path, log=lambda *_: None, portals=portals, today="2026-09-28")
    assert m and m.version == "2026-09-28" and m.dataset == "catalogue"
    first = store.source_path(tmp_path, m).read_bytes()
    assert catalogue.encode(catalogue.decode(first)) == first
    assert (
        catalogue.fetch(tmp_path, log=lambda *_: None, portals=portals, today="2026-10-05") is None
    )
    assert [r["title"] for r in catalogue.load(tmp_path)] == ["Crash data 1", "Crash data 2"]
    s.packages.append(pkg(3))
    assert (
        catalogue.fetch(tmp_path, log=lambda *_: None, portals=portals, today="2026-09-28") is None
    )
    assert store.source_path(tmp_path, m).read_bytes() == first  # the stored version is untouched


class Down(FakeSession):
    def get(self, url, params=None, timeout=None, headers=None):
        if "socrata" in url:
            r = Resp(None)
            r.content, r.headers = b"", {"Content-Type": "text/html"}
            r.json = lambda: (_ for _ in ()).throw(ValueError("empty"))
            return r
        return super().get(url, params, timeout, headers)


def test_an_unreadable_portal_keeps_its_last_records_and_says_so(tmp_path, monkeypatch):
    portals = (catalogue.BY_CODE["qld"], catalogue.BY_CODE["act"])
    monkeypatch.setattr(catalogue.requests, "Session", lambda: FakeSession([pkg(1)]))
    first = catalogue.fetch(tmp_path, log=lambda *_: None, portals=portals, today="2026-09-21")
    assert {r["portal"] for r in catalogue.load(tmp_path)} == {"qld", "act"}
    monkeypatch.setattr(catalogue.requests, "Session", lambda: Down([pkg(1), pkg(2)]))
    lines = []
    m = catalogue.fetch(tmp_path, log=lines.append, portals=portals, today="2026-09-28")
    assert m.source["stats"]["act"]["carried_from"] == "2026-09-21"
    assert "not JSON" in m.source["stats"]["act"]["error"]
    assert [r["id"] for r in catalogue.load(tmp_path) if r["portal"] == "act"] == ["act-426s-vdu4"]
    assert any("could not be read" in x for x in lines)
    # Without the earlier snapshot's bytes nothing can be carried, so the harvest stops.
    store.source_path(tmp_path, first).unlink()
    store.source_path(tmp_path, m).unlink()
    with __import__("pytest").raises(catalogue.PortalError, match="store pull --only catalogue"):
        catalogue.fetch(tmp_path, log=lambda *_: None, portals=portals, today="2026-10-05")


def test_a_first_harvest_marks_an_unreadable_portal_as_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue.requests, "Session", lambda: Down([pkg(1)]))
    portals = (catalogue.BY_CODE["qld"], catalogue.BY_CODE["act"])
    m = catalogue.fetch(tmp_path, log=lambda *_: None, portals=portals, today="2026-09-28")
    assert m.source["stats"]["act"] == {
        "records": 0,
        "error": m.source["stats"]["act"]["error"],
        "carried_from": None,
    }
    assert {r["portal"] for r in catalogue.load(tmp_path)} == {"qld"}


def test_a_harvest_that_cannot_finish_reports_failed_and_exits_non_zero(
    tmp_path, monkeypatch, capsys
):
    from publicdata import __main__ as cli

    def boom(store_dir):
        raise catalogue.PortalError("snapshot not in the store")

    monkeypatch.setattr(catalogue, "fetch", boom)
    assert cli.main(["catalogue", "fetch", "--store", str(tmp_path)]) == 1
    assert "catalogue: FAILED snapshot not in the store" in capsys.readouterr().out


FIX = Path(__file__).parent / "fixtures" / "catalogue"


class FixtureSession:
    """Serves recorded Opendatasoft and ArcGIS Hub answers (Ballarat and Sydney, 29 Sep 2026)."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        if "/api/explore/v2.1/catalog/datasets" in url:
            body = json.loads((FIX / "ods-ballarat.json").read_text(encoding="utf-8"))
            if params["offset"]:
                body["results"] = []
            return Resp(body)
        if "/api/search/v1/collections/dataset/items" in url:
            page = 2 if "startindex=3" in url else 1
            return Resp(json.loads((FIX / f"hub-sydney-{page}.json").read_text(encoding="utf-8")))
        raise AssertionError(url)


def test_opendatasoft_records_are_the_council_s_own_with_formats_from_the_platform():
    recs, dropped = catalogue.ods(
        catalogue.BY_CODE["ballarat"], FixtureSession(), log=lambda *_: None
    )
    assert dropped == 0
    by = {r["name"]: r for r in recs}
    # A dataset federated from another portal is left to that portal.
    assert len(recs) == 5 and not any("@" in n for n in by)
    # A dataset with no records is a page of links: listed, with no files to vote on.
    links = by["superseded-plans-collection"]
    assert links["formats"] == [] and not links["downloadable"] and links["licence"] == "CC-BY-4.0"
    toilets = by["public-toilets"]
    assert toilets["org"] == "ballarat" and toilets["org_title"] == "City of Ballarat"
    assert toilets["url"] == "https://data.ballarat.vic.gov.au/explore/dataset/public-toilets/"
    # "CC BY 3" names no version; the deed URL beside it does.
    assert toilets["licence"] == "CC-BY-3.0-AU" and toilets["open"] is True
    assert {"CSV", "GEOJSON", "API"} <= set(toilets["formats"]) and toilets["downloadable"]
    assert toilets["id"].startswith("ballarat-da-")
    assert by["libraries"]["licence"] == "" and by["libraries"]["open"] is None
    assert "GEOJSON" not in by["local-workers-occupation-by-industry"]["formats"]
    assert set(recs[0]) == set(catalogue.socrata(catalogue.BY_CODE["act"], FakeSession([]))[0][0])


def test_hub_pages_until_there_is_no_next_link_and_reads_licences_from_the_terms():
    s = FixtureSession()
    recs, _ = catalogue.hub(catalogue.BY_CODE["sydney"], s, log=lambda *_: None)
    # The second page is the next link as Hub gives it.
    assert [u.rsplit("/", 1)[-1] for u, _ in s.calls] == ["items", "items?limit=2&startindex=3"]
    by = {r["title"]: r for r in recs}
    trees = by["Trees"]
    assert (
        trees["url"]
        == "https://data.cityofsydney.nsw.gov.au/datasets/15c4713a688a48fcb604fc343118af05"
    )
    assert trees["licence"] == "CC-BY-4.0" and trees["downloadable"]
    assert trees["created"] == "2021-02-25" and trees["org_title"] == "City of Sydney"
    assert [catalogue._epoch_day(x) for x in (None, "None", "", "9" * 30)] == [""] * 4
    # "custom" with a Creative Commons deed linked in the terms is that licence.
    assert by["Sydney Development Control Plan 2012"]["licence"] == "CC-BY-4.0"
    assert by["Free Tree Giveaway"]["licence"] == "CC-BY-SA-4.0"
    assert by["Library details"]["licence"] == "" and by["Library details"]["open"] is None
    assert catalogue.hub_licence({"license": "custom", "licenseInfo": "<p>Terms apply.</p>"}) == (
        "custom",
        "Terms apply.",
    )


def test_a_council_portal_replaces_only_the_copies_it_lists():
    ballarat = catalogue.BY_CODE["ballarat"]
    own = catalogue._record(ballarat, source_id="da_1", name="toilets", title="Public Toilets",
                            org="ballarat", org_title="City of Ballarat", kind="dataset", licence="CC-BY-4.0",
                            licence_title="", formats=["CSV"], created="", modified="", url="", summary="",
                            harvested_from="")  # fmt: skip

    def copy(portal, i, title, host=""):
        return catalogue._record(catalogue.BY_CODE[portal], source_id=f"c{i}", name=f"c{i}", title=title,
                                 org="city-of-ballarat", org_title="City of Ballarat", kind="dataset",
                                 licence="CC-BY-4.0", licence_title="", formats=["CSV"], created="",
                                 modified="", url="", summary="", harvested_from="", source_host=host)  # fmt: skip

    same_title = copy("vic", 1, "Public toilets")
    same_source = copy("vic", 2, "Old name", "data.ballarat.vic.gov.au")
    elsewhere = copy("gov", 3, "Heritage status")
    other_org = {**copy("vic", 4, "Public Toilets"), "org": "someone-else"}
    stats = {"vic": {}, "gov": {}}
    kept = catalogue._drop_copies(
        [own, same_title, same_source, elsewhere, other_org], catalogue.PORTALS, stats
    )
    assert [r["title"] for r in kept] == ["Public Toilets", "Heritage status", "Public Toilets"]
    assert stats["vic"]["dropped_duplicates"] == 2
    # Without records of its own, nothing it replaces is dropped.
    assert len(catalogue._drop_copies([same_title, elsewhere], catalogue.PORTALS, {})) == 2


def test_a_flaky_council_keeps_its_last_records_and_never_stops_the_harvest(monkeypatch):
    class Broken(FixtureSession):
        def get(self, url, params=None, timeout=None, headers=None):
            if "ballarat" in url:
                raise catalogue.requests.ConnectionError("down")
            return super().get(url, params, timeout, headers)

    monkeypatch.setattr(catalogue.requests, "Session", Broken)
    monkeypatch.setattr(catalogue.time, "sleep", lambda *_: None)
    before = {"id": "ballarat-da-old", "portal": "ballarat", "title": "Kept", "org": "ballarat"}
    recs, stats = catalogue.harvest(
        (catalogue.BY_CODE["ballarat"], catalogue.BY_CODE["sydney"]),
        log=lambda *_: None,
        previous=[before],
        previous_version="2026-09-28",
    )
    assert stats["ballarat"]["carried_from"] == "2026-09-28" and "error" in stats["ballarat"]
    assert before in recs and sum(r["portal"] == "sydney" for r in recs) == 4


def test_a_portal_answering_an_odd_shape_is_kept_from_the_last_snapshot(monkeypatch):
    class Odd(FixtureSession):
        def get(self, url, params=None, timeout=None, headers=None):
            if "ballarat" in url:
                return Resp({"total_count": 1, "results": [None]})
            return super().get(url, params, timeout, headers)

    monkeypatch.setattr(catalogue.requests, "Session", Odd)
    before = {"id": "ballarat-da-old", "portal": "ballarat", "title": "Kept", "org": "ballarat"}
    recs, stats = catalogue.harvest(
        (catalogue.BY_CODE["ballarat"],),
        log=lambda *_: None,
        previous=[before],
        previous_version="2026-09-28",
    )
    assert recs == [before] and "error" in stats["ballarat"]


def test_every_portal_code_fits_a_vote_key_and_publishers_place_councils_locally():
    import re

    from publicdata.publishers import PORTAL_JUR, load_curated, resolve

    assert all(re.fullmatch(r"[a-z]+", p.code) for p in catalogue.PORTALS)
    assert len({p.code for p in catalogue.PORTALS}) == len(catalogue.PORTALS)
    replaced = [o for p in catalogue.PORTALS for o in p.replaces]
    assert len(set(replaced)) == len(replaced), "two portals replace the same organisation"
    councils = [p for p in catalogue.PORTALS if p.kind in ("ods", "hub")]
    recs = [{"portal": p.code, "org": p.code, "org_title": p.publisher} for p in councils]
    curated = load_curated(Path(__file__).parents[2] / "register" / "publishers")
    pubs, by_org = resolve(recs, curated, {p.code: p.jurisdiction for p in catalogue.PORTALS})
    for p in councils:
        pub = by_org[f"{p.code}:{p.code}"]
        assert pub.jurisdiction == PORTAL_JUR[p.jurisdiction] and pub.level == "local", p.code
    # A council already curated from another portal gains the new portal's records.
    assert by_org["melb:melb"] is by_org["vic:city-of-melbourne"]
