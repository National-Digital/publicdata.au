import xml.etree.ElementTree as ET
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


def test_ecat_licences_come_from_the_licence_each_record_cites() -> None:
    by = by_title()
    # GA cites its licence by title with "CC-BY", "4.0", an addressee and a copyright line, and
    # marks the resource unclassified in a security constraint beside it.
    geoid = by["AUSGeoid98 v.1.0 data fi"]
    assert geoid["licence"] == "CC-BY-4.0"
    assert geoid["open"] is True
    assert geoid["licence_title"].startswith(
        "Creative Commons Attribution 4.0 International Licence"
    )
    # A record with GA's licence id and no citation names no licence in its own words.
    quake = by["Vanuatu Earthquake 17 De"]
    assert quake["licence"] == catalogue.ASSESSING
    assert "Commonwealth of Australia (Geoscience Australia)" in quake["licence_title"]


SYSTEM = "Australian Government Security Classification System"
CITED = {
    "alternate": "CC-BY",
    "edition": "4.0",
    "addressee": "Any",
    "release": "",
    "dissemination": "",
    "use": '<gmd:MD_RestrictionCode codeListValue="license"/>',
    "access": '<gmd:MD_RestrictionCode codeListValue="license">license</gmd:MD_RestrictionCode>',
    "other": "© Commonwealth of Australia (Geoscience Australia) 2025",
    "security": "unclassified",
    "security_text": "",
    "security_title": SYSTEM,
    "system": "Australian Government Security ClassificationSystem",
    "note": "",
}


def cs(t: str) -> str:
    return f"<gco:CharacterString>{t}</gco:CharacterString>"


def cited(**kw: str) -> ET.Element:
    """GA's licence and security constraints as eCat writes them, with fields replaced by kw."""
    f = CITED | kw
    dissemination = (
        f'<gmd:disseminationConstraints><gmd:MD_RestrictionCode codeListValue="{f["dissemination"]}"/>'
        "</gmd:disseminationConstraints>"
        if f["dissemination"]
        else ""
    )
    note = f"<gmd:userNote>{cs(f['note'])}</gmd:userNote>" if f["note"] else ""
    return ET.fromstring(  # noqa: S314 - the test's own literal
        '<gmd:MD_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd" '
        'xmlns:gco="http://www.isotc211.org/2005/gco"><gmd:identificationInfo>'
        "<gmd:MD_DataIdentification><gmd:resourceConstraints>"
        '<gmd:MD_LegalConstraints uuid="urn:ga-licences:232"><gmd:useLimitation/><gmd:reference>'
        "<gmd:CI_Citation>"
        f"<gmd:title>{cs('Creative Commons Attribution 4.0 International Licence')}</gmd:title>"
        f"<gmd:alternateTitle>{cs(f['alternate'])}</gmd:alternateTitle>"
        f'<gmd:date gco:nilReason="missing"/><gmd:edition>{cs(f["edition"])}</gmd:edition>'
        "</gmd:CI_Citation></gmd:reference><gmd:releasability><gmd:MD_Releasability>"
        "<gmd:addressee><gmd:CI_ResponsibleParty>"
        f"<gmd:individualName>{cs(f['addressee'])}</gmd:individualName>"
        f"<gmd:positionName>{cs(f['addressee'])}</gmd:positionName><gmd:role><gmd:CI_RoleCode "
        'codeListValue="user">user</gmd:CI_RoleCode></gmd:role></gmd:CI_ResponsibleParty>'
        f"</gmd:addressee><gmd:statement>{cs(f['release'])}</gmd:statement>{dissemination}"
        "</gmd:MD_Releasability></gmd:releasability>"
        f"<gmd:useConstraints>{f['use']}</gmd:useConstraints>"
        f"<gmd:accessConstraints>{f['access']}</gmd:accessConstraints>"
        f"<gmd:otherConstraints>{cs(f['other'])}</gmd:otherConstraints>"
        "</gmd:MD_LegalConstraints></gmd:resourceConstraints><gmd:resourceConstraints>"
        "<gmd:MD_SecurityConstraints><gmd:reference><gmd:CI_Citation>"
        f"<gmd:title>{cs(f['security_title'])}</gmd:title></gmd:CI_Citation></gmd:reference>"
        "<gmd:classification><gmd:MD_ClassificationCode "
        f'codeListValue="{f["security"]}">{f["security_text"]}</gmd:MD_ClassificationCode>'
        f"</gmd:classification>{note}"
        f"<gmd:classificationSystem>{cs(f['system'])}</gmd:classificationSystem>"
        "</gmd:MD_SecurityConstraints></gmd:resourceConstraints></gmd:MD_DataIdentification>"
        "</gmd:identificationInfo></gmd:MD_Metadata>"
    )


def lic(**kw: str) -> str:
    return catalogue.csw_licence(cited(**kw))[0]


def test_a_cited_licence_is_read_in_ecat_s_shape() -> None:
    assert lic() == "CC-BY-4.0"
    assert lic(alternate="CC BY", other="(c) State of New South Wales 2020") == "CC-BY-4.0"
    assert lic(other="Copyright Geoscience Australia 2019", addressee="Any user") == "CC-BY-4.0"
    assert lic(system=SYSTEM, security_text="unclassified") == "CC-BY-4.0"


def test_a_cited_licence_with_other_terms_beside_it_is_held_or_closed() -> None:
    # An edition that is not the licence's version, or a short form of another licence.
    assert lic(edition="3.0") == catalogue.ASSESSING
    assert lic(alternate="CC BY-NC") == catalogue.ASSESSING
    # A copyright line names a holder and a year, and nothing more.
    assert lic(other="© Commonwealth, non-commercial use 2025") == catalogue.ASSESSING
    assert lic(other="© Commonwealth of Australia 2025. All rights reserved") == catalogue.ASSESSING
    assert lic(other="© Commonwealth, third party material excepted 2025") == catalogue.ASSESSING
    assert lic(other="Not to be used for navigation") == catalogue.ASSESSING
    # Use and access constraints hold one code that adds no terms.
    assert lic(access=cs("Non-commercial only")) == catalogue.ASSESSING
    assert lic(use=f'<gmd:MD_RestrictionCode codeListValue="license"/>{cs("NC")}') == (
        catalogue.ASSESSING
    )
    assert lic(use='<gmd:MD_RestrictionCode codeListValue="trademark"/>') == catalogue.ASSESSING
    assert lic(use='<gmd:MD_RestrictionCode codeListValue="restricted"/>') == "restricted"
    # Releasability names everyone, and no statement or dissemination constraint.
    assert lic(addressee="Australian Government agencies") == catalogue.ASSESSING
    assert lic(release="Australian Government use only") == catalogue.ASSESSING
    assert lic(dissemination="unrestricted") == catalogue.ASSESSING


def test_a_restriction_code_no_list_names_holds_a_stated_licence() -> None:
    def stated(code: str) -> str:
        m = ET.fromstring(  # noqa: S314 - the test's own literal
            '<gmd:MD_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd" '
            'xmlns:gco="http://www.isotc211.org/2005/gco"><gmd:identificationInfo>'
            "<gmd:MD_DataIdentification><gmd:resourceConstraints><gmd:MD_LegalConstraints>"
            f'<gmd:useConstraints><gmd:MD_RestrictionCode codeListValue="{code}"/>'
            f"</gmd:useConstraints><gmd:otherConstraints>{cs('CC BY 4.0')}</gmd:otherConstraints>"
            "</gmd:MD_LegalConstraints></gmd:resourceConstraints></gmd:MD_DataIdentification>"
            "</gmd:identificationInfo></gmd:MD_Metadata>"
        )
        return catalogue.csw_licence(m)[0]

    assert stated("unrestricted") == "CC-BY-4.0"
    assert stated("licenceUnrestricted") == "CC-BY-4.0"
    assert stated("trademark") == catalogue.ASSESSING
    assert stated("private") == "private"


def test_only_an_unclassified_marking_under_the_government_system_states_no_terms() -> None:
    assert lic(security="confidential") == "confidential"
    assert lic(security="sensitive") == catalogue.ASSESSING
    assert lic(security_text="OFFICIAL: Sensitive") == catalogue.ASSESSING
    assert lic(security_text="Sensitive: Cabinet") == catalogue.ASSESSING
    assert lic(system="Protective Security Policy Framework") == catalogue.ASSESSING
    assert lic(security_title="Handling instructions") == catalogue.ASSESSING
    assert lic(note="Release to government agencies only") == catalogue.ASSESSING


def test_ecat_format_names_that_are_protocols_read_as_protocols() -> None:
    by = by_title()
    # The grid names WWW:LINK, OGC:WMS and OGC:WCS as formats; only the services are formats.
    assert by["GSNSW Exploration NSW Ar"]["formats"] == ["API", "NCSS", "WMS"]
    assert by["GSNSW Exploration NSW Ar"]["downloadable"]
    m = ET.fromstring(  # noqa: S314 - the test's own literal
        '<gmd:MD_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd" '
        'xmlns:gco="http://www.isotc211.org/2005/gco"><gmd:distributionInfo><gmd:MD_Distribution>'
        + "".join(
            f"<gmd:distributionFormat><gmd:MD_Format><gmd:name><gco:CharacterString>{n}"
            "</gco:CharacterString></gmd:name></gmd:MD_Format></gmd:distributionFormat>"
            for n in (
                "OGC:WFS",
                "WWW:DOWNLOAD-1.0-http--download",
                "ESRI Shapefile",
                "ESRI:Shapefile",
            )
        )
        + "<gmd:transferOptions><gmd:MD_DigitalTransferOptions><gmd:onLine><gmd:CI_OnlineResource>"
        "<gmd:linkage><gmd:URL>https://example.gov.au/data</gmd:URL></gmd:linkage>"
        "</gmd:CI_OnlineResource></gmd:onLine></gmd:MD_DigitalTransferOptions></gmd:transferOptions>"
        "</gmd:MD_Distribution></gmd:distributionInfo></gmd:MD_Metadata>"
    )
    # A name with a colon that is no known protocol and no WWW link is kept as named.
    assert catalogue.csw_formats(m) == ["API", "ESRI:SHAPEFILE", "SHP"]


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
