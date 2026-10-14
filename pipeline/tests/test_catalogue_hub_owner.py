import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from publicdata import catalogue
from publicdata.publishers import Publisher, load_curated, resolve, suggest

from .test_catalogue import FIX, FixtureSession, Resp, session

if TYPE_CHECKING:
    import requests

    from publicdata.catalogue import Params
    from publicdata.publishers import CatalogueRecord

# Hubs of many bodies as a portal list would hold them: no single publisher named.
ACTMAPI = catalogue.Portal(
    "actmapi",
    "actmapi-actgov.opendata.arcgis.com",
    "https://actmapi-actgov.opendata.arcgis.com",
    "act",
    "hub",
)
ATLAS = catalogue.Portal(
    "atlas", "digital.atlas.gov.au", "https://digital.atlas.gov.au", "cth", "hub"
)
REGISTER = Path(__file__).parents[2] / "register" / "publishers"


class HubSession:
    def __init__(self, fixture: str) -> None:
        self.fixture = fixture

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        assert url.endswith("/api/search/v1/collections/dataset/items"), url
        return Resp(json.loads((FIX / self.fixture).read_text(encoding="utf-8")))


def _hub(portal: catalogue.Portal, fixture: str) -> list[catalogue.Record]:
    s = cast("requests.Session", HubSession(fixture))
    return catalogue.hub(portal, s, log=lambda *_: None)[0]


def test_a_hub_of_many_bodies_files_each_item_under_the_body_its_source_names() -> None:
    recs = _hub(ACTMAPI, "hub-actmapi.json")
    by = {r["title"]: r for r in recs}
    species = by["ACTGOV Threatened Native Species List"]
    assert species["org"] == "city-and-environment-directorate-act-government"
    assert species["org_title"] == "City and Environment Directorate | ACT Government"
    barriers = by["ACTGOV Road Barrier Assets"]
    assert barriers["org"] == "australian-capital-territory-government"
    assert barriers["org_title"] == "Australian Capital Territory Government"
    # The owner is an account name and is never published as the body.
    owners = {"environment-actgov", "cityoperationsactgov"}
    assert not owners & {catalogue._org_slug(r["org_title"]) for r in recs}
    assert barriers["licence"] == "CC-BY-4.0"
    assert barriers["url"].startswith("https://actmapi-actgov.opendata.arcgis.com/datasets/")
    sydney = catalogue.hub(
        catalogue.BY_CODE["sydney"], session(FixtureSession()), log=lambda *_: None
    )[0]
    assert set(species) == set(sydney[0])


def test_an_item_with_no_source_is_filed_as_unknown_on_its_hub() -> None:
    assert catalogue.hub_org(ACTMAPI, {"id": "x", "source": "  "}) == ("", "")
    assert catalogue.hub_org(ACTMAPI, {"id": "x"}) == ("", "")


def test_every_council_hub_keeps_its_one_publisher() -> None:
    hubs = [p for p in catalogue.PORTALS if p.kind == "hub"]
    assert hubs
    assert all(catalogue.hub_org(p, {"id": "x", "source": "Esri"}) is None for p in hubs)
    recs = catalogue.hub(
        catalogue.BY_CODE["sydney"], session(FixtureSession()), log=lambda *_: None
    )[0]
    assert {(r["org"], r["org_title"]) for r in recs} == {("sydney", "City of Sydney")}


def test_hub_bodies_become_publishers_in_the_hub_s_jurisdiction_or_as_curated() -> None:
    recs: list[CatalogueRecord] = [
        *_hub(ACTMAPI, "hub-actmapi.json"),
        *_hub(ATLAS, "hub-digitalatlas.json"),
    ]
    act = Publisher(
        "act-government",
        "ACT Government",
        "ACT",
        "state",
        orgs=["actmapi:australian-capital-territory-government"],
        curated=True,
    )
    curated = [*load_curated(REGISTER), act]
    jur = {p.code: p.jurisdiction for p in (*catalogue.PORTALS, ACTMAPI, ATLAS)}
    _pubs, by_org = resolve(recs, curated, jur)
    assert by_org["actmapi:australian-capital-territory-government"] is act
    ced = by_org["actmapi:city-and-environment-directorate-act-government"]
    assert (ced.jurisdiction, ced.level) == ("ACT", "state")
    # A body the curated list already names under the same title gains the hub's records.
    ga = by_org["atlas:geoscience-australia"]
    assert ga.curated
    assert ga.path == "/cth/geoscience-australia/"


def test_publishers_proposes_curation_for_a_hub_s_bodies() -> None:
    recs: list[CatalogueRecord] = [
        *_hub(ACTMAPI, "hub-actmapi.json"),
        *_hub(ATLAS, "hub-digitalatlas.json"),
        {
            "portal": "act",
            "org": "act-government",
            "org_title": "ACT Government",
            "kind": "dataset",
        },
        {
            "portal": "gov",
            "org": "data-act",
            "org_title": "Government of The ACT - Australian Capital Territory Government",
            "kind": "dataset",
        },
    ]
    jur = {p.code: p.jurisdiction for p in (*catalogue.PORTALS, ACTMAPI, ATLAS)}
    assert {s["org"] for s in suggest(recs, [], {}, jur)} == {"gov:data-act"}
    rows = {s["org"]: s for s in suggest(recs, [], {}, jur, ["actmapi", "atlas"])}
    ced = rows["actmapi:city-and-environment-directorate-act-government"]
    assert (ced["jurisdiction"], ced["level"]) == ("ACT", "state")
    # A body the default places as it stands is not proposed.
    assert "atlas:geoscience-australia" not in rows
    # A hub body is a merge target for an aggregator's copy, never for itself.
    assert rows["gov:data-act"]["merge"] == "actmapi:australian-capital-territory-government"
    assert rows["actmapi:australian-capital-territory-government"]["merge"] == ""


def test_a_copy_of_a_council_portal_s_body_is_proposed_as_a_merge_with_it() -> None:
    recs: list[CatalogueRecord] = [
        {"portal": "bne", "org": "bne", "org_title": "Brisbane City Council", "kind": "dataset"},
        {"portal": "gov", "org": "bcc", "org_title": "Brisbane City Council", "kind": "dataset"},
    ]
    jur = {p.code: p.jurisdiction for p in catalogue.PORTALS}
    (row,) = suggest(recs, [], {"Brisbane": "Qld"}, jur)
    assert (row["org"], row["jurisdiction"], row["merge"]) == ("gov:bcc", "Qld", "bne:bne")
