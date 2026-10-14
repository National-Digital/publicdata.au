import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from publicdata import catalogue
from publicdata.publishers import load_curated, resolve

if TYPE_CHECKING:
    import requests

    from publicdata.catalogue import Params
    from publicdata.publishers import CatalogueRecord

FIX = Path(__file__).parent / "fixtures" / "catalogue"
REPO = Path(__file__).parents[2]


class Resp:
    def __init__(self, body: object) -> None:
        self.body = body
        self.status_code = 200

    def json(self) -> object:
        return self.body

    def raise_for_status(self) -> None:
        pass


class HubSession:
    """Serves six DCCEEW Hub items recorded on 14 Oct 2026, descriptions shortened."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        self.calls.append(url)
        return Resp(json.loads((FIX / "hub-dcceew.json").read_text(encoding="utf-8")))


def test_the_dcceew_hub_is_read_with_the_department_as_publisher() -> None:
    s = HubSession()
    portal = catalogue.BY_CODE["dcceew"]
    recs, dropped = catalogue.hub(portal, cast("requests.Session", s), log=lambda *_: None)
    assert s.calls == ["https://fed.dcceew.gov.au/api/search/v1/collections/dataset/items"]
    assert dropped == 0
    by = {r["title"]: r for r in recs}
    assert len(by) == 6
    zones = by["Underwater Cultural Heritage Protection Zones"]
    assert zones["url"] == "https://fed.dcceew.gov.au/datasets/9eb26ad044b24c8cac589e92241ee9a0"
    assert zones["org"] == "dcceew"
    assert zones["org_title"] == "Department of Climate Change, Energy, the Environment and Water"
    assert zones["formats"] == ["FGDB"]
    assert zones["downloadable"]
    # Hub's licence list has no Australian ports; the deed in the terms names the one meant.
    assert zones["licence"] == "CC-BY-3.0-AU"
    assert zones["open"] is True
    vectors = by[
        "National Vegetation Information System (NVIS) Version 7.0 - Extant Vectors Download"
    ]
    assert vectors["licence"] == "CC-BY-4.0"
    reserves = by["Commonwealth Marine Reserves 2014 - (not current)"]
    assert reserves["licence"] == "CC-BY-NC-3.0-AU"
    assert reserves["open"] is False
    assert {"API", "GEOJSON"} <= set(reserves["formats"])
    # "none" with no terms states no licence.
    assert by["IUCN Categories"]["licence"] == ""
    assert by["IUCN Categories"]["open"] is None
    assert by["IUCN Categories"]["formats"] == ["CSV"]
    # Terms that name CC BY are read as CC BY; terms that name no licence are held closed.
    capad = by["Collaborative Australian Protected Areas Database (CAPAD) 2024 - Marine"]
    assert capad["licence"] == "CC-BY"
    snes = by[
        "Species of National Environmental Significance and selected marine and cetacean species - GDB"
    ]
    assert snes["licence"] == "custom"
    assert snes["open"] is False


def test_the_hub_replaces_nothing_and_its_records_join_the_department_s_publisher() -> None:
    portal = catalogue.BY_CODE["dcceew"]
    assert portal.jurisdiction == "cth"
    assert portal.replaces == ()
    assert "fed.dcceew.gov.au" in catalogue.DIRECT_HOSTS
    recs: list[CatalogueRecord] = [
        {"portal": "dcceew", "org": "dcceew", "org_title": portal.publisher},
        {
            "portal": "gov",
            "org": "dcceew",
            "org_title": "Australian Government Department of Climate Change, Energy, the Environment and Water",
        },
    ]
    curated = load_curated(REPO / "register" / "publishers")
    _pubs, by_org = resolve(recs, curated, {p.code: p.jurisdiction for p in catalogue.PORTALS})
    pub = by_org["dcceew:dcceew"]
    assert by_org["gov:dcceew"] is pub
    assert (pub.jurisdiction, pub.level, pub.short) == ("Cth", "federal", "DCCEEW")
    # The page data.gov.au's records already sit on keeps its address.
    assert (
        pub.path
        == "/cth/australian-government-department-of-climate-change-energy-the-environment-and/"
    )
