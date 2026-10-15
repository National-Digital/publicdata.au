import json
from typing import TYPE_CHECKING, cast

import requests

from publicdata import catalogue
from publicdata.directory import plan, render

from .test_catalogue import FIX, FixtureSession, Resp

if TYPE_CHECKING:
    import pytest

    from publicdata.catalogue import Params
    from publicdata.jsontypes import JSON, JSONObject


SEED, NSW, SYDNEY = (catalogue.BY_CODE[c] for c in ("seed", "nsw", "sydney"))


def _fixture(name: str) -> JSONObject:
    return cast("JSONObject", json.loads((FIX / name).read_text(encoding="utf-8")))


# The fake answers only the calls the CKAN harvester makes.
def session(fake: SeedSession) -> requests.Session:
    return cast("requests.Session", fake)


class SeedSession:
    """Serves answers recorded from SEED and data.nsw on 14 Oct 2026, trimmed to a few records.

    The cc-by record is SEED's package_show answer cut to the fields package_search returns.
    """

    def __init__(
        self, packages: str = "seed-packages.json", extra: list[JSON] | None = None
    ) -> None:
        self.body = _fixture(packages)
        result = self.body["result"]
        assert isinstance(result, dict)
        results, count = result["results"], result["count"]
        assert isinstance(results, list)
        assert isinstance(count, int)
        results += extra or []
        result["count"] = count + len(extra or [])
        self.headers: dict[str, str] = {}
        self.posts: list[JSONObject | None] = []

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        if url.endswith("/license_list"):
            return Resp(_fixture("seed-license-list.json"))
        if url.endswith("/organization_list"):
            # SEED answers a GET here with HTTP 400 and asks for a POST.
            assert "seed.nsw" not in url
            return Resp({"result": []})
        if url.endswith("/package_search"):
            assert params is not None
            assert "extras_harvest_url" in str(params["fl"])
            return Resp(
                self.body if not params["start"] else {"result": {"count": 0, "results": []}}
            )
        raise AssertionError(url)

    def post(
        self,
        url: str,
        json: JSONObject | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        assert url.endswith("/organization_list"), url
        self.posts.append(json)
        assert json is not None
        return Resp(_fixture("seed-organizations.json") if json["offset"] == 0 else {"result": []})


def test_seed_reads_its_own_licence_ids_and_holds_the_ones_nobody_has_read() -> None:
    s = SeedSession()
    recs, dropped = catalogue.ckan(catalogue.BY_CODE["seed"], session(s), log=lambda *_: None)
    assert dropped == 0
    # organization_list goes by POST, one page and the empty one after it.
    assert [p and p["offset"] for p in s.posts] == [0, 25]
    by = {r["name"]: r for r in recs}
    # SEED's cc-by is titled "Creative Commons Attribution 4.0" and links the 4.0 deed.
    soil = by["soil-and-land-resources-of-the-hawkesbury-nepean-catchment2bef0"]
    assert (soil["licence"], soil["open"]) == ("CC-BY-4.0", True)
    assert soil["url"] == (
        "https://datasets.seed.nsw.gov.au/dataset/"
        "soil-and-land-resources-of-the-hawkesbury-nepean-catchment2bef0"
    )
    assert soil["id"] == "seed-2ea220d3-d396-4a61-8cc8-7c9c1cd9a7bc"
    # cc-at-3 is titled "Creative Commons Attribution 3.0" and links the AU deed.
    poi = by["nsw-points-of-interest-poi"]
    assert (poi["licence"], poi["open"]) == ("CC-BY-3.0-AU", True)
    assert poi["licence_title"] == "Creative Commons Attribution 3.0"
    assert poi["downloadable"]
    assert "API" in poi["formats"]
    # SEED's agreement text for its third-party licence allows personal, non-commercial use only.
    third = by["barham-frmsp-report-figures"]
    assert (third["licence"], third["open"]) == ("3rd party licence", False)
    oeh = next(r for r in recs if r["licence_title"] == "OEH licence")
    assert (oeh["licence"], oeh["open"]) == ("OEH licence", False)
    nc_nd = by["water-modelling-modelled-data-regional-water-strategy-clarence"]
    assert (nc_nd["licence"], nc_nd["open"]) == ("CC-BY-NC-ND-4.0", False)
    assert {r["org"] for r in recs} >= {"spatial-services", "murray-river-council"}
    assert all(r["harvested_from"] == "" for r in recs)


KEPT: JSONObject = {
    "id": "00000000-aaaa-bbbb-cccc-000000000001",
    "name": "kept",
    "title": "Kept",
    "organization": "x",
    "license_id": "cc-by",
    "harvest_url": "https://opengov.nsw.gov.au/publications/1",
}


def test_data_nsw_marks_the_copies_it_names_a_portal_read_here_as_the_source_of() -> None:
    s = SeedSession("nsw-copies.json", [KEPT])
    recs, dropped = catalogue.ckan(catalogue.BY_CODE["nsw"], session(s), log=lambda *_: None)
    # Three SEED copies. City of Sydney's copy is left to that portal's replaces rule, and a portal
    # not read here keeps its copy.
    assert dropped == 3
    copies = {r["name"]: r.get("copy_of") for r in recs}
    assert copies["kept"] is None
    assert copies["nsw-points-of-interest-poi"] == (
        "https://datasets.seed.nsw.gov.au/dataset/nsw-points-of-interest-poi"
    )
    assert copies["5-cityofsydney--sydney-development-control-plan-2012"] is None
    assert sum(c is not None for c in copies.values()) == 3
    assert not s.posts


class TwoPortals(SeedSession):
    """SEED's recorded answers and data.nsw's recorded copies, each at its own portal."""

    def __init__(self) -> None:
        super().__init__()
        self.nsw = SeedSession("nsw-copies.json", [KEPT]).body

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        if "/api/search/v1/" in url:
            return FixtureSession().get(url, params, timeout, headers)
        if url.startswith(NSW.api) and url.endswith("/package_search"):
            assert params is not None
            return Resp(
                self.nsw if not params["start"] else {"result": {"count": 0, "results": []}}
            )
        return super().get(url, params, timeout, headers)


def test_a_vote_on_data_nsw_s_copy_of_a_seed_record_counts_for_the_seed_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests, "Session", TwoPortals)
    recs, stats = catalogue.harvest((SEED, NSW), log=lambda *_: None)
    by_id = {r["id"]: r for r in recs}
    voted = "nsw-addf72ca-e5f7-445a-a717-6b5a230531d7"
    poi = "seed-004adc52-39bb-4eb1-a659-b33cc0e703f4"
    assert voted not in by_id
    assert by_id[poi]["copies"] == [voted]
    # The two other SEED copies name records the SEED fixture does not hold, so data.nsw keeps
    # them, as it does a copy whose SEED original has gone.
    kept = {r["name"] for r in recs if r["portal"] == "nsw"}
    assert "melville-range-nature-reserve-vegetation-2006-vis_id-4746" in kept
    assert "kept" in kept
    assert all("copy_of" not in r for r in recs)
    assert stats["nsw"] == {"records": 4, "dropped_duplicates": 1}
    # The SEED records with no copy in the fixture list none.
    assert sum("copies" in r for r in recs) == 1
    written: dict[str, str] = {}
    render(plan([], recs, [], "2026-10-14"), lambda *a, **k: None, written.__setitem__, {},
           lambda c: {})  # fmt: skip
    copies = {k: v for k, v in written.items() if k.startswith("catalogue/copies/")}
    assert {c: r for v in copies.values() for c, r in json.loads(v).items()} == {voted: poi}
    shard = next(v for k, v in written.items() if k.startswith("catalogue/votable/") and poi in v)
    assert json.loads(shard)[poi][4] == [voted]


def test_data_gov_au_still_keeps_records_it_holds_first_hand() -> None:
    gov = catalogue.BY_CODE["gov"]
    own: catalogue._CkanPackage = {"id": "x"}
    site = "https://data.gov.au/data/dataset/"
    assert catalogue._copy_of(gov, own, {"site_url": site}) is None
    assert catalogue._copy_of(gov, own, {"site_url": "https://www.data.nsw.gov.au/"}) == ""
    page = "https://datasets.seed.nsw.gov.au/dataset/x"
    assert catalogue._copy_of(gov, own, {"site_url": page, "href": page}) == page
    # Only data.gov.au's own origin field counts as a copy; the other portals name one in
    # harvest_url.
    assert catalogue._copy_of(NSW, own, {"site_url": "https://data.gov.au/"}) is None
    assert catalogue._copy_of(SEED, {"id": "y", "harvest_url": page}, {}) is None


def test_data_gov_au_keeps_a_record_whose_harvest_url_names_another_portal() -> None:
    # The shape of gov-017670b2-..., which names data.sa in harvest_url and no origin.
    sa: catalogue._CkanPackage = {
        "id": "z",
        "harvest_url": "https://data.sa.gov.au/data/dataset/victorian-microbreweries",
    }
    assert catalogue._copy_of(catalogue.BY_CODE["gov"], sa, {}) is None


def test_data_nsw_s_copy_of_a_city_of_sydney_record_follows_the_hub_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests, "Session", TwoPortals)
    recs, stats = catalogue.harvest((SYDNEY, NSW), log=lambda *_: None)
    copy = "nsw-890610de-6e10-465a-8e7d-e45246a7ed08"
    dcp = next(r for r in recs if r["title"] == "Sydney Development Control Plan 2012")
    assert dcp["portal"] == "sydney"
    assert dcp["copies"] == [copy]
    assert copy not in {r["id"] for r in recs}
    assert stats["nsw"]["dropped_duplicates"] == 1
