from pathlib import Path
from typing import TYPE_CHECKING, cast

from publicdata import catalogue
from publicdata.publishers import load_curated, resolve

if TYPE_CHECKING:
    import requests

    from publicdata.catalogue import Params, Record
    from publicdata.store import PortalStats

FIX = Path(__file__).parent / "fixtures" / "catalogue"
GA = catalogue.BY_CODE["ga"]


class XmlResp:
    def __init__(self, body: bytes) -> None:
        self.content = body
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass


class EcatSession:
    """Serves GA eCat's GetRecords answer, recorded 14 Oct 2026 and trimmed to three records."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[Params] = []

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> XmlResp:
        assert url == GA.api
        assert params is not None
        self.calls.append(params)
        assert len(self.calls) == 1, "paged past the last record"
        return XmlResp((FIX / "csw-ga.xml").read_bytes())


def harvest() -> list[Record]:
    s = EcatSession()
    recs, dropped = catalogue.csw(GA, cast("requests.Session", s), log=lambda *_: None)
    assert dropped == 0
    assert s.calls[0]["startPosition"] == 1
    return recs


def by_title() -> dict[str, Record]:
    return {r["title"][:24]: r for r in harvest()}


def test_ecat_records_are_read_onto_the_catalogue_shape() -> None:
    by = by_title()
    assert len(by) == 3
    grid = by["GSNSW Exploration NSW Ar"]
    assert grid["id"] == "ga-000ca07b-8761-4555-b0cf-a5fc86011a58"
    assert grid["url"] == GA.landing("000ca07b-8761-4555-b0cf-a5fc86011a58")
    assert grid["url"].startswith("https://ecat.ga.gov.au/geonetwork/srv/eng/")
    assert grid["kind"] == "dataset"
    assert by["Vanuatu Earthquake 17 De"]["kind"] == "document"
    assert by["AUSGeoid98 v.1.0 data fi"]["formats"] == ["DAT", "ZIP"]
    assert "ecat.ga.gov.au" in catalogue.DIRECT_HOSTS


def test_ecat_parties_all_place_under_the_curated_geoscience_australia() -> None:
    recs = harvest()
    # The AUSGeoid98 record names an internal unit, MNHD, as custodian ahead of the agency.
    assert {r["org"] for r in recs} <= {
        "commonwealth-of-australia-geoscience-australia", "geoscience-australia", "mnhd"
    }  # fmt: skip
    curated = load_curated(Path(__file__).parents[2] / "register" / "publishers")
    _pubs, by_org = resolve(recs, curated, {p.code: p.jurisdiction for p in catalogue.PORTALS})
    for r in recs:
        pub = by_org[f"ga:{r['org']}"]
        assert pub.slug == "geoscience-australia"
        assert pub.curated
        assert (pub.jurisdiction, pub.level) == ("Cth", "federal")


def test_ecat_licences_are_held_for_review_until_the_citation_form_is_read() -> None:
    by = by_title()
    # GA cites its licence by title, alternate title "CC-BY" and edition "4.0", beside a
    # copyright line; the words beside the title hold the record for review.
    geoid = by["AUSGeoid98 v.1.0 data fi"]
    assert geoid["licence"] == catalogue.ASSESSING
    assert "Creative Commons Attribution 4.0 International Licence" in geoid["licence_title"]
    assert geoid["open"] is False
    # A record with GA's licence id and no citation names no licence in its own words.
    quake = by["Vanuatu Earthquake 17 De"]
    assert quake["licence"] == catalogue.ASSESSING
    assert "Commonwealth of Australia (Geoscience Australia)" in quake["licence_title"]


def gov(i: int, title: str, org: str = "geoscience-australia-data") -> Record:
    return catalogue._record(catalogue.BY_CODE["gov"], source_id=f"c{i}", name=f"c{i}", title=title,
                             org=org, org_title="Geoscience Australia Data", kind="dataset",
                             licence="", licence_title="notspecified", formats=["HTML"], created="",
                             modified="", url="", summary="", harvested_from="")  # fmt: skip


def test_ecat_replaces_data_gov_au_copies_by_title_and_keeps_records_it_does_not_hold() -> None:
    own = harvest()
    geoid = next(r for r in own if r["title"].startswith("AUSGeoid98"))
    # data.gov.au holds several copies of one eCat record, under names ending -1, -2 and so on.
    copies = [
        gov(1, "AUSGeoid98 v.1.0 data files: (sl54-02)"),
        gov(2, "AUSGeoid98 v.1.0 data files:  (sl54-02)"),
    ]
    gone = gov(3, "A record eCat no longer lists")
    own_org = gov(4, geoid["title"], org="geoscienceaustralia")
    stats: dict[str, PortalStats] = {"gov": {}}
    moved: dict[str, str] = {}
    kept = catalogue._drop_copies([*own, *copies, gone, own_org], catalogue.PORTALS, stats, moved)
    assert not [c for c in copies if c in kept]
    assert gone in kept
    # GA's own organisation on data.gov.au states licences of its own, so it is left alone.
    assert own_org in kept
    assert stats["gov"]["dropped_duplicates"] == 2
    assert moved == {c["id"]: geoid["id"] for c in copies}
    by = {r["id"]: r for r in catalogue._carry(kept, [], moved, [])}
    assert by[geoid["id"]]["copies"] == sorted(c["id"] for c in copies)
