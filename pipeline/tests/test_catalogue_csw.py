import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import requests

from publicdata import catalogue, directory
from publicdata.publishers import load_curated, resolve

if TYPE_CHECKING:
    from publicdata.catalogue import Params, Record
    from publicdata.store import PortalStats

FIX = Path(__file__).parent / "fixtures" / "catalogue"
THELIST = catalogue.BY_CODE["thelist"]


class XmlResp:
    def __init__(self, body: bytes) -> None:
        self.content = body
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass


class CswSession:
    """Serves theLIST's GetRecords answers, recorded 14 Oct 2026 and trimmed to four records.

    The counts are rewritten to fit the four, so the paging ends after the second page.
    """

    def __init__(self, pages: dict[int, bytes] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[Params] = []
        self.pages = pages or {
            1: (FIX / "csw-thelist-1.xml").read_bytes(),
            4: (FIX / "csw-thelist-2.xml").read_bytes(),
        }

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> XmlResp:
        assert url == THELIST.api
        assert params is not None
        self.calls.append(params)
        assert len(self.calls) < 10, "paged without end"
        return XmlResp(self.pages[cast("int", params["startPosition"])])


def harvest(s: CswSession) -> list[Record]:
    recs, dropped = catalogue.csw(THELIST, cast("requests.Session", s), log=lambda *_: None)
    assert dropped == 0
    return recs


def metadata(constraints: str = "", distribution: str = "", after: str = "") -> ET.Element:
    return ET.fromstring(  # noqa: S314 - the test's own literal
        '<gmd:MD_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd" '
        'xmlns:gco="http://www.isotc211.org/2005/gco" xmlns:gmx="http://www.isotc211.org/2005/gmx" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        f"<gmd:identificationInfo><gmd:MD_DataIdentification>{constraints}"
        "</gmd:MD_DataIdentification></gmd:identificationInfo>"
        f"<gmd:distributionInfo><gmd:MD_Distribution>{distribution}</gmd:MD_Distribution>"
        f"</gmd:distributionInfo>{after}</gmd:MD_Metadata>"
    )


def legal(*statements: str, code: str = "otherRestrictions") -> str:
    said = "".join(f"<gmd:otherConstraints>{x}</gmd:otherConstraints>" for x in statements)
    return (
        "<gmd:resourceConstraints><gmd:MD_LegalConstraints><gmd:useConstraints>"
        f'<gmd:MD_RestrictionCode codeListValue="{code}"/></gmd:useConstraints>{said}'
        "</gmd:MD_LegalConstraints></gmd:resourceConstraints>"
    )


def text(s: str) -> str:
    return f"<gco:CharacterString>{s}</gco:CharacterString>"


def test_csw_pages_on_next_record_and_asks_for_iso_records_in_a_stable_order() -> None:
    s = CswSession()
    recs = harvest(s)
    assert [c["startPosition"] for c in s.calls] == [1, 4]
    first = s.calls[0]
    assert first["outputSchema"] == "http://www.isotc211.org/2005/gmd"
    assert first["typeNames"] == "gmd:MD_Metadata"
    assert first["resultType"] == "results"
    assert first["maxRecords"] == catalogue.CSW_PAGE
    assert first["sortBy"] == "Identifier:A"
    assert len(recs) == 4
    by = {r["title"]: r for r in recs}
    area = by["LIST Built Up Area"]
    assert area["id"] == "thelist-008d8ecd-6451-4172-9b89-c0f70109dd04"
    # theLIST runs GeoNetwork 2, which serves no catalog.search.
    assert area["url"] == (
        "https://data.thelist.tas.gov.au/datagn/srv/eng/metadata.show?uuid="
        "008d8ecd-6451-4172-9b89-c0f70109dd04"
    )
    # Two records' pages compare as different pages, and a pasted page finds its record.
    keys = {catalogue.bare_url(r["url"]) for r in recs}
    assert len(keys) == len(recs)
    by_id = {r["id"]: r for r in recs}
    assert directory.find(area["url"], {}, by_id, {}, {THELIST.host: "thelist"}) is area
    ga = "https://ecat.ga.gov.au/geonetwork/srv/eng/catalog.search#/metadata/"
    assert catalogue.bare_url(ga + "1") != catalogue.bare_url(ga + "2")
    # Each record names its own custodian; theLIST holds other agencies' and councils' data.
    assert area["org_title"] == "Land Tasmania"
    assert by["Sewer Points - Launceston City Council Detail Survey Database"]["org"] == (
        "launceston-city-council"
    )
    fossicking = by["Fossicking Areas (Mineral Resources Tasmania)"]
    assert fossicking["created"] == "2020-07-30"
    assert fossicking["modified"] == "2026-09-08"
    assert fossicking["summary"].startswith("Declared fossicking areas")
    assert set(recs[0]) == set(
        catalogue._record(THELIST, source_id="x", name="x", title="x", org="x", org_title="x",
                          kind="dataset", licence="", licence_title="", formats=[], created="",
                          modified="", url="", summary="", harvested_from="")
    )  # fmt: skip


def test_csw_stops_on_a_last_page_whose_next_record_is_past_the_count() -> None:
    # GeoNetwork's last page gives nextRecord one past the count, not 0.
    last = (FIX / "csw-thelist-2.xml").read_bytes().replace(b'nextRecord="0"', b'nextRecord="5"')
    s = CswSession({1: (FIX / "csw-thelist-1.xml").read_bytes(), 4: last})
    assert len(harvest(s)) == 4
    assert len(s.calls) == 2


def test_csw_stops_when_next_record_fails_to_advance() -> None:
    page = (FIX / "csw-thelist-1.xml").read_bytes().replace(b'nextRecord="4"', b'nextRecord="1"')
    with pytest.raises(catalogue.PortalError, match="does not move past"):
        harvest(CswSession({1: page}))


def test_csw_fails_on_an_empty_page_before_the_count_is_reached() -> None:
    empty = (
        b'<csw:GetRecordsResponse xmlns:csw="http://www.opengis.net/cat/csw/2.0.2">'
        b'<csw:SearchResults numberOfRecordsMatched="4" numberOfRecordsReturned="0" nextRecord="0"/>'
        b"</csw:GetRecordsResponse>"
    )
    s = CswSession({1: (FIX / "csw-thelist-1.xml").read_bytes(), 4: empty})
    with pytest.raises(catalogue.PortalError, match="empty of 4"):
        harvest(s)


def test_csw_reports_an_exception_report_and_refuses_an_external_entity() -> None:
    report = (
        b'<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows"><ows:Exception '
        b'exceptionCode="MissingParameterValue" locator="constraintLanguage"/></ows:ExceptionReport>'
    )
    with pytest.raises(catalogue.PortalError, match="MissingParameterValue"):
        harvest(CswSession({1: report}))
    entity = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/hostname">]>'
        b'<csw:GetRecordsResponse xmlns:csw="http://www.opengis.net/cat/csw/2.0.2">&e;'
        b"</csw:GetRecordsResponse>"
    )
    with pytest.raises(catalogue.PortalError, match="not XML"):
        harvest(CswSession({1: entity}))
    # Expat expands internal entities but refuses the amplification of a billion laughs.
    lol = b"".join(
        b'<!ENTITY l%d "%s">' % (i, b"&l%d;" % (i - 1) * 10 if i else b"lol") for i in range(10)
    )
    laughs = b"<?xml version='1.0'?><!DOCTYPE r [" + lol + b"]><r>&l9;</r>"
    with pytest.raises(catalogue.PortalError, match="amplification"):
        harvest(CswSession({1: laughs}))


def test_csw_licences_match_known_wordings_and_hold_anything_else_for_review() -> None:
    by = {r["title"]: r for r in harvest(CswSession())}
    # theLIST writes the data licence in the metadata constraints and none on the resource.
    area = by["LIST Built Up Area"]
    assert area["licence"] == "CC-BY-3.0-AU"
    assert area["open"] is True
    sewer = by["Sewer Points - Launceston City Council Detail Survey Database"]
    assert sewer["licence"] == catalogue.ASSESSING
    assert sewer["open"] is False
    assert "licensing agreement" in sewer["licence_title"]
    # Empty statements are no statement.
    assert by["Soil pH"]["licence"] == ""
    assert by["Soil pH"]["open"] is None

    def lic(*statements: str, code: str = "otherRestrictions") -> str:
        return catalogue.csw_licence(metadata(legal(*map(text, statements), code=code)))[0]

    assert lic("Creative Commons Attribution 4.0 International Licence") == "CC-BY-4.0"
    assert lic("CC-BY 4.0") == "CC-BY-4.0"
    assert lic("Other Licence", "CC BY 3.0 AU") == "CC-BY-3.0-AU"
    assert lic("https://creativecommons.org/licenses/by/4.0/") == "CC-BY-4.0"
    # Any other words beside a licence may narrow it, so the record is held, with every word kept.
    licence, said = catalogue.csw_licence(
        metadata(
            legal(text("CC BY 4.0"), text("Not for commercial use. Third party material excluded."))
        )
    )
    assert licence == "assessing"
    assert said == "CC BY 4.0 Not for commercial use. Third party material excluded."
    # A deed URL counts only as a link or a statement of its own, never inside a sentence.
    assert (
        lic(
            "Available for non-commercial use only, see https://creativecommons.org/licenses/by/4.0/"
        )
        == "assessing"
    )
    anchor = (
        '<gmx:Anchor xlink:href="https://creativecommons.org/licenses/by-nc/4.0/">'
        "Creative Commons Attribution 4.0 International</gmx:Anchor>"
    )
    assert catalogue.csw_licence(metadata(legal(anchor)))[0] == "assessing"
    deed = '<gmx:Anchor xlink:href="https://creativecommons.org/licenses/by-nc/4.0/"/>'
    assert catalogue.csw_licence(metadata(legal(deed)))[0] == "CC-BY-NC-4.0"
    assert catalogue.is_open("CC-BY-NC-4.0") is False
    # A sentence around a licence name is not one of the wordings, and two licences conflict.
    assert lic("Released under Creative Commons Attribution 4.0 except where noted") == "assessing"
    assert lic("CC BY 4.0", "Creative Commons Attribution 3.0 Australia") == "assessing"
    assert lic("CC BY 4.0", code="restricted") == "restricted"
    assert catalogue.is_open("restricted") is False


def test_only_codes_that_add_no_terms_leave_a_licence_open_and_only_a_deed_host_names_one() -> None:
    def lic(*statements: str, code: str = "otherRestrictions") -> str:
        return catalogue.csw_licence(metadata(legal(*map(text, statements), code=code)))[0]

    for code in sorted(catalogue.CSW_OPEN_CODES["MD_RestrictionCode"]):
        assert lic("CC BY 4.0", code=code) == "CC-BY-4.0"
    # A code no list names, such as a trademark or a patent, may carry terms of its own.
    assert lic("CC BY 4.0", code="trademark") == "assessing"
    assert lic("CC BY 4.0", code="patentPending") == "assessing"
    assert lic("CC BY 4.0", code="private") == "private"
    assert lic("https://www.creativecommons.org/licenses/by/4.0/") == "CC-BY-4.0"
    # A deed path inside another site's URL names no licence.
    assert lic("https://example.org/x?to=creativecommons.org/licenses/by/4.0") == "assessing"
    deed = '<gmx:Anchor xlink:href="https://example.org/creativecommons.org/licenses/by/4.0/"/>'
    assert catalogue.csw_licence(metadata(legal(deed)))[0] == "assessing"


def test_metadata_constraints_count_only_when_the_record_has_no_resource_constraints() -> None:
    meta = (
        "<gmd:metadataConstraints><gmd:MD_LegalConstraints><gmd:useLimitation>"
        f"{text('Creative Commons Attribution 3.0 Australia Licence')}</gmd:useLimitation>"
        "</gmd:MD_LegalConstraints></gmd:metadataConstraints>"
    )

    def lic(resource: str) -> str:
        return catalogue.csw_licence(metadata(resource, after=meta))[0]

    assert catalogue.csw_licence(metadata(after=meta))[0] == "CC-BY-3.0-AU"
    assert lic(legal(text("CC BY 4.0"))) == "CC-BY-4.0"
    # Each of these states terms on the resource, so the metadata's licence is not read.
    link = '<gmx:Anchor xlink:href="https://example.gov.au/terms-of-use"/>'
    assert lic(legal(link)) == "assessing"
    free = (
        '<gmd:PT_FreeText><gmd:textGroup><gmd:LocalisedCharacterString locale="#EN">'
        "Attribution-NonCommercial 4.0 International</gmd:LocalisedCharacterString>"
        "</gmd:textGroup></gmd:PT_FreeText>"
    )
    assert lic(legal(free)) == "assessing"
    # A translated statement counts as a statement beside a licence named in English.
    assert lic(legal(text("CC BY 4.0"), free)) == "assessing"
    classified = (
        "<gmd:resourceConstraints><gmd:MD_SecurityConstraints><gmd:classification>"
        '<gmd:MD_ClassificationCode codeListValue="confidential"/></gmd:classification>'
        "</gmd:MD_SecurityConstraints></gmd:resourceConstraints>"
    )
    assert lic(classified) == "confidential"
    assert lic("<gmd:resourceConstraints/>") == "assessing"


def online(url: str, protocol: str = "") -> str:
    proto = f"<gmd:protocol>{text(protocol)}</gmd:protocol>" if protocol else ""
    return (
        "<gmd:transferOptions><gmd:MD_DigitalTransferOptions><gmd:onLine><gmd:CI_OnlineResource>"
        f"<gmd:linkage><gmd:URL>{url}</gmd:URL></gmd:linkage>{proto}"
        "</gmd:CI_OnlineResource></gmd:onLine></gmd:MD_DigitalTransferOptions></gmd:transferOptions>"
    )


def test_csw_formats_come_from_named_formats_protocols_and_linked_files() -> None:
    by = {r["title"]: r for r in harvest(CswSession())}
    # theLIST links a map viewer and a PDF of notes: listed, with nothing to download.
    assert by["Soil pH"]["formats"] == ["PDF"]
    assert not by["Soil pH"]["downloadable"]
    assert by["LIST Built Up Area"]["formats"] == []
    named = (
        "<gmd:distributionFormat><gmd:MD_Format><gmd:name>"
        f"{text('ESRI Shapefile')}</gmd:name></gmd:MD_Format></gmd:distributionFormat>"
    )
    # A named format with no link to fetch it from is no download.
    assert catalogue.csw_formats(metadata(distribution=named)) == []
    shp = named.replace("ESRI Shapefile", "ESRI_shp") + named.replace("ESRI Shapefile", "Other")
    zipped = online("https://example.gov.au/files/roads.zip")
    assert catalogue.csw_formats(metadata(distribution=shp + zipped)) == ["SHP", "ZIP"]
    m = metadata(
        distribution=named
        + online("https://example.gov.au/geoserver/wms", "OGC:WMS-1.3.0-http-get-map")
        + online("https://example.gov.au/geoserver/wfs", "OGC:WFS")
        + online("https://example.gov.au/arcgis/rest/services/Roads/MapServer")
        + online("https://example.gov.au/files/roads.csv.zip", "WWW:DOWNLOAD-1.0-http--download")
        + online("https://example.gov.au/metadata/roads.xml")
    )
    assert catalogue.csw_formats(m) == ["API", "SHP", "WMS", "ZIP"]


def test_dates_in_the_older_records_format_are_read_and_odd_ones_left_blank() -> None:
    assert catalogue._csw_day("Fri Dec 20 09:34:48 EST 2013") == "2013-12-20"
    assert catalogue._csw_day("2021-12-13T12:24:57.514+11:00") == "2021-12-13"
    assert catalogue._csw_day("sometime in 2013") == ""


def test_thelist_replaces_data_gov_au_copies_by_title_and_keeps_the_rest() -> None:
    def gov(i: int, title: str, org: str = "tas-government-the-list") -> Record:
        return catalogue._record(catalogue.BY_CODE["gov"], source_id=f"c{i}", name=f"c{i}", title=title,
                                 org=org, org_title="Tasmania Government's The List Data", kind="dataset",
                                 licence="", licence_title="notspecified", formats=["HTML"], created="",
                                 modified="", url="", summary="", harvested_from="")  # fmt: skip

    own = harvest(CswSession())
    # data.gov.au's search index states no harvest source for these, so the host rule misses them.
    copy = gov(1, "LIST built up area")
    kelp = gov(
        2, "A Survey of the Macrocystis pyrifera Stocks on the East Coast of Tasmania, 1988."
    )
    other = gov(3, "Soil pH", org="city-of-hobart-open-data")
    stats: dict[str, PortalStats] = {"gov": {}}
    moved: dict[str, str] = {}
    kept = catalogue._drop_copies([*own, copy, kelp, other], catalogue.PORTALS, stats, moved)
    assert copy not in kept
    assert kelp in kept
    assert other in kept
    assert stats["gov"]["dropped_duplicates"] == 1
    assert "data.thelist.tas.gov.au" in catalogue.DIRECT_HOSTS
    # The copy's id moves to theLIST's record, so a vote cast on the copy counts there.
    area = "thelist-008d8ecd-6451-4172-9b89-c0f70109dd04"
    assert moved == {copy["id"]: area}
    by = {r["id"]: r for r in catalogue._carry(kept, [], moved, [])}
    assert by[area]["copies"] == [copy["id"]]
    assert "copies" not in by[kelp["id"]]
    # A title theLIST lists twice names no single record, so the copy is dropped and carried nowhere.
    twin: Record = {**own[0], "id": "thelist-twin"}
    moved = {}
    catalogue._drop_copies([*own, twin, copy], catalogue.PORTALS, {}, moved)
    assert moved == {}


def test_thelist_records_place_their_publishers_in_tasmania() -> None:
    recs = harvest(CswSession())
    curated = load_curated(Path(__file__).parents[2] / "register" / "publishers")
    _pubs, by_org = resolve(recs, curated, {p.code: p.jurisdiction for p in catalogue.PORTALS})
    assert {by_org[f"thelist:{r['org']}"].jurisdiction for r in recs} == {"Tas"}
    # Land Tasmania is already curated from data.gov.au and gains theLIST's records.
    assert by_org["thelist:land-tasmania"] is by_org["gov:land-tasmania"]
    assert by_org["thelist:launceston-city-council"].level == "local"


def test_a_failed_csw_portal_keeps_its_last_records(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(CswSession):
        def get(
            self,
            url: str,
            params: Params | None = None,
            timeout: float | None = None,
            headers: dict[str, str] | None = None,
        ) -> XmlResp:
            return XmlResp(b"<html>maintenance")

    monkeypatch.setattr(requests, "Session", Broken)
    before = cast("Record", {"id": "thelist-old", "portal": "thelist", "title": "Kept", "org": "x"})
    recs, stats = catalogue.harvest(
        (THELIST,), log=lambda *_: None, previous=[before], previous_version="2026-10-12"
    )
    assert recs == [before]
    assert "not XML" in stats["thelist"]["error"]


def party(org: str, role: str) -> str:
    return (
        f"<gmd:CI_ResponsibleParty><gmd:organisationName>{text(org)}</gmd:organisationName>"
        f'<gmd:role><gmd:CI_RoleCode codeListValue="{role}"/></gmd:role></gmd:CI_ResponsibleParty>'
    )


def test_the_resource_s_own_party_names_the_publisher_before_the_metadata_contact() -> None:
    m = metadata(
        f"<gmd:pointOfContact>{party('Mineral Resources Tasmania', 'author')}</gmd:pointOfContact>"
        f"<gmd:pointOfContact>{party('Land Tasmania', 'custodian')}</gmd:pointOfContact>",
        after=f"<gmd:contact>{party('Metadata Office', 'publisher')}</gmd:contact>",
    )
    assert catalogue._csw_party(m, "fallback") == "Land Tasmania"
    alone = metadata(after=f"<gmd:contact>{party('Metadata Office', 'publisher')}</gmd:contact>")
    assert catalogue._csw_party(alone, "fallback") == "Metadata Office"
    assert catalogue._csw_party(metadata(), "fallback") == "fallback"


def test_csw_fails_on_a_read_that_ends_short_of_the_count_or_matches_nothing() -> None:
    # A first page that gives no next record ends the read with one page of the four.
    page = (FIX / "csw-thelist-1.xml").read_bytes().replace(b'nextRecord="4"', b'nextRecord="0"')
    with pytest.raises(catalogue.PortalError, match="read 3 of the 4"):
        harvest(CswSession({1: page}))
    # A catalogue that matches nothing while its index rebuilds keeps its last records.
    none = (
        b'<csw:GetRecordsResponse xmlns:csw="http://www.opengis.net/cat/csw/2.0.2">'
        b'<csw:SearchResults numberOfRecordsMatched="0" numberOfRecordsReturned="0" nextRecord="0"/>'
        b"</csw:GetRecordsResponse>"
    )
    with pytest.raises(catalogue.PortalError, match="read 0 of the 0"):
        harvest(CswSession({1: none}))
