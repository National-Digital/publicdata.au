import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import requests

from publicdata import catalogue
from publicdata.directory import plan, render, shard
from publicdata.publishers import load_curated, resolve, suggest

from .test_catalogue import FIX, FakeSession, Resp, pkg
from .test_catalogue_hub_owner import HubSession

if TYPE_CHECKING:
    from publicdata.catalogue import HubItem, Params, Record
    from publicdata.publishers import CatalogueRecord

GOV, ACTMAPI, ATLAS = (catalogue.BY_CODE[c] for c in ("gov", "actmapi", "atlas"))
REGISTER = Path(__file__).parents[2] / "register" / "publishers"
# data.gov.au's two copies of the ACTmapi toilets item, a copy of one layer of the canopy item,
# and a copy of an item the Hub's dataset search does not list.
TOILETS = ("gov-1671a502-f4fb-4c88-aa61-818f30c1fabb", "gov-4d39be76-3d37-4111-938b-92037f0b1a51")
CANOPY = "gov-6d1a2d8c-c796-4c89-b0b1-1e8ed0bf6f01"
LIDAR = "gov-c00aaa8c-b12c-4dca-95d0-38007d35d378"
TOILET_ITEM = "actmapi-5fbbda15bc254a0ead86fa34edb12e56"
CANOPY_ITEM = "actmapi-2f277dfd24964ef3943d99b34c38ebad"


class GovAndActmapi(FakeSession):
    """data.gov.au answers its recorded ACTmapi copies; ACTmapi answers the items they copy."""

    def __init__(
        self, extra: list[dict[str, object]], *, hub_down: bool = False, links: bool = True
    ) -> None:
        recorded = json.loads((FIX / "ckan-gov-actmapi.json").read_text(encoding="utf-8"))
        super().__init__([*recorded["result"]["results"], *extra])
        self.hub_down, self.links = hub_down, links
        self.asked: list[Params] = []

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        if url.startswith(ACTMAPI.api):
            if self.hub_down:
                msg = "down"
                raise requests.ConnectionError(msg)
            return HubSession("hub-actmapi-copies.json").get(url, params, timeout, headers)
        if params and params.get("fl") == "id,res_url":
            self.asked.append(params)
            if not self.links:
                msg = "down"
                raise requests.ConnectionError(msg)
            return Resp(json.loads((FIX / "ckan-gov-actmapi-res-url.json").read_text("utf-8")))
        return super().get(url, params, timeout, headers)


def _hub(portal: catalogue.Portal, fixture: str) -> list[Record]:
    return catalogue.hub(portal, cast("requests.Session", HubSession(fixture)), lambda *_: None)[0]


def test_actmapi_and_the_digital_atlas_are_hubs_of_many_bodies() -> None:
    assert (ACTMAPI.kind, ACTMAPI.publisher, ACTMAPI.jurisdiction) == ("hub", "", "act")
    assert (ATLAS.kind, ATLAS.publisher, ATLAS.jurisdiction) == ("hub", "", "cth")
    assert ACTMAPI.copied_by == ("gov:act-government-geospatial-data-catalogue",)
    # data.gov.au holds no harvest of the Digital Atlas, so nothing of its is dropped.
    assert not ATLAS.copied_by
    assert not ATLAS.replaces
    taken = [o for p in catalogue.PORTALS for o in (*p.replaces, *p.copied_by)]
    assert len(set(taken)) == len(taken), "two portals drop the same organisation"


def test_every_body_on_the_two_hubs_lands_on_its_publisher() -> None:
    sources = json.loads((FIX / "hub-sources.json").read_text(encoding="utf-8"))
    recs: list[CatalogueRecord] = []
    for code, answer in sources.items():
        (agg,) = answer["aggregations"]["aggregations"]
        recs += [
            {"portal": code, "org": catalogue._org_slug(a["label"]), "org_title": a["label"],
             "kind": "dataset"}
            for a in agg["aggregations"]
        ]  # fmt: skip
    assert len(recs) == 18 + 3
    curated = load_curated(REGISTER)
    jur = {p.code: p.jurisdiction for p in catalogue.PORTALS}
    _pubs, by_org = resolve(recs, curated, jur)
    want = {
        "actmapi:australian-capital-territory-government": "/act/act-government/",
        "actmapi:city-and-environment-directorate-act-government": "/act/city-and-environment/",
        "actmapi:city-and-environment-directorate": "/act/city-and-environment/",
        "atlas:australian-bureau-of-statistics": "/cth/australian-bureau-of-statistics/",
        "atlas:geoscience-australia": "/cth/geoscience-australia/",
        "atlas:digital-earth-australia": "/cth/geoscience-australia/",
        "atlas:digital-atlas-of-australia": "/cth/geoscience-australia/",
        "atlas:australian-bureau-of-agricultural-and-resource-economics-and-sciences": "/cth/australian-bureau-of-agricultural-and-resource-economics-and-sciences/",
        "atlas:bureau-of-meteorology": "/cth/bureau-of-meteorology/",
        "atlas:department-of-climate-change-energy-the-environment-and-water": "/cth/australian-government-department-of-climate-change-energy-the-environment-and/",
        "atlas:australian-taxation-office": "/cth/australian-taxation-office/",
        "atlas:bureau-of-infrastructure-and-transport-research-economics": "/cth/bitre/",
        "atlas:department-of-industry-science-and-resources": "/cth/department-of-industry-science-and-resources-disr/",
        "atlas:department-of-health-disability-and-ageing": "/cth/department-of-health-disability-and-ageing/",
        "atlas:department-of-social-services": "/cth/department-of-social-services/",
        "atlas:geoscape-australia": "/cth/geoscape/",
        "atlas:australian-communications-and-media-authority": "/cth/australian-communications-and-media-authority/",
        "atlas:australian-electoral-commission": "/cth/australian-electoral-commission/",
        "atlas:department-of-infrastructure-transport-regional-development-communications-sport-and-the-arts": "/cth/department-of-infrastructure/",
        "atlas:national-indigenous-australians-agency": "/cth/national-indigenous-australians-agency/",
        "atlas:national-native-title-tribunal": "/cth/national-native-title-tribunal/",
    }  # fmt: skip
    assert {k: by_org[k].path for k in want} == want
    assert {by_org[k].level for k in want if k.startswith("actmapi:")} == {"state"}
    # Geoscape Australia is a company the governments own, not an agency.
    assert by_org["atlas:geoscape-australia"].level == "other"
    assert {by_org[k].level for k in want if k != "atlas:geoscape-australia"} <= {
        "federal",
        "state",
    }
    rows = suggest(recs, curated, {}, jur, ["actmapi", "atlas"])
    assert not [r for r in rows if r["org"].split(":")[0] in ("actmapi", "atlas")]


def test_data_gov_au_s_actmapi_copies_map_by_item_id_and_their_votes_follow_the_hub_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same title in an organisation that is not the harvester's is the publisher's own.
    other = pkg(5, title="ACTGOV Public Toilet Assets")
    session = GovAndActmapi([other])
    monkeypatch.setattr(requests, "Session", lambda: session)
    recs, stats = catalogue.harvest((GOV, ACTMAPI), log=lambda *_: None)
    # One extra search per copying organisation, for the download links alone.
    assert session.asked == [
        {"fq": 'organization:"act-government-geospatial-data-catalogue"', "fl": "id,res_url",
         "rows": 1000}
    ]  # fmt: skip
    by_id = {r["id"]: r for r in recs}
    assert sorted(by_id) == sorted([CANOPY_ITEM, TOILET_ITEM, LIDAR, f"gov-{other['id']}"])
    # One toilets copy names the item in its links; the map copy is matched on its title.
    assert by_id[TOILET_ITEM]["copies"] == sorted(TOILETS)
    # A copy of one layer has its own title, and its links name the multi-layer item.
    assert by_id[CANOPY_ITEM]["copies"] == [CANOPY]
    # A copy of an item the Hub's dataset search does not list is kept and counted.
    assert "copies" not in by_id[LIDAR]
    assert stats["gov"]["dropped_duplicates"] == len(TOILETS) + 1
    assert stats["gov"]["unmatched_copies"] == 1
    written: dict[str, str] = {}
    render(plan([], recs, load_curated(REGISTER), "2026-10-15"), lambda *a, **k: None,
           written.__setitem__, {}, lambda c: {})  # fmt: skip
    for c in TOILETS:
        assert json.loads(written[f"catalogue/copies/{shard(c)}.json"])[c] == TOILET_ITEM
    entry = json.loads(written[f"catalogue/votable/{shard(TOILET_ITEM)}.json"])[TOILET_ITEM]
    assert entry[1] == "/act/act-government/"
    assert entry[4] == sorted(TOILETS)


def test_without_the_download_links_copies_match_on_title_and_the_rest_stay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(requests, "Session", lambda: GovAndActmapi([], links=False))
    recs, stats = catalogue.harvest((GOV, ACTMAPI), log=lambda *_: None)
    by_id = {r["id"]: r for r in recs}
    assert by_id[TOILET_ITEM]["copies"] == sorted(TOILETS)
    assert CANOPY in by_id
    assert LIDAR in by_id
    assert stats["gov"]["unmatched_copies"] == 2


def test_the_copies_stay_while_actmapi_has_no_records_in_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    session = GovAndActmapi([], hub_down=True)
    monkeypatch.setattr(requests, "Session", lambda: session)
    recs, stats = catalogue.harvest((GOV, ACTMAPI), log=lambda *_: None)
    assert sorted(r["id"] for r in recs) == sorted([*TOILETS, CANOPY, LIDAR])
    assert stats["actmapi"]["records"] == 0
    assert not session.asked


def test_actmapi_alone_reads_no_copies_from_data_gov_au(monkeypatch: pytest.MonkeyPatch) -> None:
    session = GovAndActmapi([])
    monkeypatch.setattr(requests, "Session", lambda: session)
    recs, _ = catalogue.harvest((ACTMAPI,), log=lambda *_: None)
    assert TOILET_ITEM in {r["id"] for r in recs}
    assert not session.asked


class Partial(HubSession):
    """A Hub search that answers fewer items than it matched, or flags a partial page."""

    def __init__(self, fixture: str, **change: object) -> None:
        super().__init__(fixture)
        self.change = change

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        got = super().get(url, params, timeout, headers)
        return Resp({**cast("dict[str, object]", got.body), **self.change})


def test_a_hub_search_that_stops_short_is_an_error_so_its_records_are_kept() -> None:
    for change in ({"numberMatched": 3}, {"exceededTransferLimit": True}):
        s = cast("requests.Session", Partial("hub-actmapi-copies.json", **change))
        with pytest.raises(catalogue.PortalError):
            catalogue.hub(ACTMAPI, s, lambda *_: None)
    # Pages that repeat one item and skip another still answer as many features as matched.
    page = json.loads((FIX / "hub-actmapi-copies.json").read_text(encoding="utf-8"))
    twice = [page["features"][0]] * 2
    s = cast("requests.Session", Partial("hub-actmapi-copies.json", features=twice))
    with pytest.raises(catalogue.PortalError):
        catalogue.hub(ACTMAPI, s, lambda *_: None)
    s = cast("requests.Session", Partial("hub-actmapi-copies.json"))
    assert len(catalogue.hub(ACTMAPI, s, lambda *_: None)[0]) == 2


def test_a_hub_search_is_read_in_a_stable_order() -> None:
    s = FakeSession([])
    asked: list[Params | None] = []

    def get(url: str, params: Params | None = None, *_: object, **__: object) -> Resp:
        asked.append(params)
        return Resp({"features": [], "numberMatched": 0, "links": []})

    s.get = get  # type: ignore[method-assign]
    catalogue.hub(ACTMAPI, cast("requests.Session", s), lambda *_: None)
    assert asked[0] is not None
    assert asked[0]["sortBy"] == "properties.created"


def test_the_hubs_custom_terms_are_read_as_they_state() -> None:
    toilets = _hub(ACTMAPI, "hub-actmapi-copies.json")[0]
    # ACTmapi's custom terms name CC BY 4.0, so its public toilets can be voted for.
    assert (toilets["licence"], toilets["open"], toilets["downloadable"]) == (
        "CC-BY-4.0",
        True,
        True,
    )
    by = {r["title"]: r for r in _hub(ATLAS, "hub-digitalatlas-bodies.json")}
    got = {t: (r["licence"], r["open"]) for t, r in by.items()}
    assert got == {
        # The deed link carries a port.
        "ABS Australian population grid 2023": ("CC-BY-4.0", True),
        # The terms grant CC BY for most data owners only.
        "Monitoring Sites - Rainfall": ("custom", False),
        "Heavy Vehicle Median Speed for 2022 by Hour of the Day": ("CC-BY", True),
        "DEA Land Cover Level 3": ("CC-BY-4.0", True),
        "National Roads": ("CC-BY-4.0", True),
        "Indigenous Ranger Groups": ("CC-BY-NC-4.0", False),
    }  # fmt: skip
    assert catalogue.hub_licence(
        {"id": "x", "license": "custom", "licenseInfo": "Creative Commons Attribution 4.0"}
    ) == ("CC-BY-4.0", "Creative Commons Attribution 4.0")


def test_hub_licences_are_decided_from_the_whole_of_the_terms() -> None:
    preamble = "Use of this data is subject to the terms below. " * 8
    late: HubItem = {
        "id": "x",
        "license": "custom",
        "licenseInfo": f"<p>{preamble}</p><p>CC BY 4.0</p>",
    }
    lid, text = catalogue.hub_licence(late)
    assert lid == "CC-BY-4.0"
    assert len(text) <= catalogue.SUMMARY_CHARS + 1
    # A stated open id is held when the terms say it covers only some of the data.
    stated: HubItem = {"id": "x", "license": "CC-BY-4.0",
              "licenseInfo": "CC BY 4.0 applies to most data providers' material."}  # fmt: skip
    assert catalogue.hub_licence(stated)[0] == "custom"
    assert catalogue.hub_licence({**stated, "licenseInfo": "CC BY 4.0."})[0] == "CC-BY-4.0"
    assert catalogue.cc_url("https://creativecommons.org:443/licenses/by/4.0/") == "CC-BY-4.0"


def test_long_custom_terms_name_a_licence_only_by_a_versioned_cc_name_and_never_when_withheld() -> (
    None
):
    preamble = "Use of this data is subject to the terms below. " * 8

    def custom(terms: str) -> str:
        item: HubItem = {"id": "x", "license": "custom", "licenseInfo": f"<p>{preamble}</p>{terms}"}
        return catalogue.hub_licence(item)[0]

    assert custom("<p>The data supports Net zero targets.</p>") == "custom"
    assert custom("<p>Material in the public domain elsewhere is cited.</p>") == "custom"
    assert custom("<p>See the Creative Commons website for more.</p>") == "custom"
    assert custom("<p>Some layers use CC BY 4.0.</p>") == "custom"
    assert custom("<p>This data is not licensed under Creative Commons Attribution 4.0.</p>") == (
        "custom"
    )
    assert custom("<p>It is not in the public domain. CC BY 4.0 does not apply.</p>") == "custom"
    assert custom("<p>All rights reserved. Creative Commons Attribution 4.0</p>") == "custom"
    assert custom("<p>Creative Commons Attribution 4.0 for non-commercial use only.</p>") == (
        "custom"
    )
    assert custom("<p>Creative Commons Attribution 4.0. No derivative works.</p>") == "custom"
    assert custom("<p>CC BY 4.0 applies. The data may not be redistributed.</p>") == "custom"
    assert custom("<p>Some of our data owners use CC BY 4.0.</p>") == "custom"
    assert custom("<p>Licensed under Creative Commons Attribution 4.0 International.</p>") == (
        "CC-BY-4.0"
    )
    assert custom("<p>CC BY-NC 4.0</p>") == "CC-BY-NC-4.0"
