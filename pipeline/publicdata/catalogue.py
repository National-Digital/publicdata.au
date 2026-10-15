"""The catalogue: every dataset record on Australia's government open-data portals.

Each portal's own metadata is read and re-keyed onto one record shape. Nothing is scored or
merged. A record data.gov.au copied from a state portal read here is dropped so the original is
the only copy. The snapshot is stored like any source: one file per harvest date, a new version
only when the bytes change.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, NotRequired, TypedDict, Unpack, cast
from urllib.parse import quote, urlparse

import requests

from . import store

if TYPE_CHECKING:
    from collections.abc import Callable, Container, Iterable, Mapping, Sequence
    from pathlib import Path

    from .jsontypes import JSON, JSONObject
    from .store import PortalStats

    type Log = Callable[[str], object]
    type Params = Mapping[str, str | int | float | Sequence[str]]


class _Fields(TypedDict):
    """The fields of a record each harvester gives _record."""

    name: str
    title: str
    org: str
    org_title: str
    kind: str
    licence_title: str
    created: str
    modified: str
    url: str
    summary: str
    harvested_from: str


class _CouncilFields(TypedDict):
    """The fields a council portal's harvester gives _council_record."""

    source_id: str
    name: str
    title: str
    licence: str
    licence_title: str
    formats: list[str]
    modified: str
    url: str
    summary: str


class Record(_Fields):
    """A catalogue record, keyed as _record writes it and as the snapshot reads back."""

    id: str
    portal: str
    licence: str
    open: bool | None
    formats: list[str]
    downloadable: bool
    source_host: NotRequired[str]
    files: NotRequired[list[str]]
    # The ids of the copies on other portals this record replaced, so their votes are its votes.
    copies: NotRequired[list[str]]
    # Each of those copies' pages, so a register entry that named a copy finds this record.
    copy_pages: NotRequired[dict[str, str]]
    # On a copy only, until harvest drops it: the page of the original it was harvested from.
    copy_of: NotRequired[str]
    # On a DAP collection: its version and publication stamp, the files its full record offers the
    # public when that version was read, and the day a stated embargo on them ends, so a later
    # harvest reads the full record again only on a change or once the embargo is over.
    source_version: NotRequired[str]
    file_count: NotRequired[int]
    public_from: NotRequired[str]


# The parts of each portal's API answers that the harvesters read.
class _Named(TypedDict, total=False):
    id: str
    name: str
    title: str | None


class _NamedList(TypedDict):
    result: list[_Named]


class _Harvest(TypedDict, total=False):
    site_url: str | None
    href: str | None


class _CkanOrg(TypedDict, total=False):
    name: str | None


class _CkanPackage(TypedDict, total=False):
    id: str
    name: str | None
    title: str | None
    original_harvest_source: str | _Harvest | None
    extras_original_harvest_source: str | _Harvest | None
    license_id: str | None
    organization: str | _CkanOrg | None
    dataset_type: str | None
    type: str | None
    res_format: list[object] | None
    metadata_created: str | None
    metadata_modified: str | None
    url: str | None
    notes: str | None


class _CkanResult(TypedDict):
    results: list[_CkanPackage]
    count: int


class _CkanSearch(TypedDict):
    result: _CkanResult


class _SocrataResource(TypedDict, total=False):
    id: str
    type: str | None
    attribution: str | None
    name: str | None
    createdAt: str | None
    data_updated_at: str | None
    updatedAt: str | None
    description: str | None


class _SocrataMeta(TypedDict, total=False):
    license: str | None


class _SocrataResult(TypedDict, total=False):
    resource: _SocrataResource
    metadata: _SocrataMeta | None
    permalink: str | None
    link: str | None


class _SocrataPage(TypedDict):
    results: list[_SocrataResult]
    resultSetSize: int


class _Dataflow(TypedDict, total=False):
    id: str
    version: str | None
    name: str | None
    description: str | None


class _Dataflows(TypedDict):
    dataflows: list[_Dataflow]


class _SdmxAnswer(TypedDict):
    data: _Dataflows


class _OdsMeta(TypedDict, total=False):
    license: str | None
    license_url: str | None
    title: str | None
    modified: str | None
    description: str | None


class _OdsMetas(TypedDict, total=False):
    default: _OdsMeta | None


class _OdsDataset(TypedDict, total=False):
    dataset_id: str
    dataset_uid: str | None
    metas: _OdsMetas | None
    has_records: bool | None
    features: list[str] | None


class _OdsPage(TypedDict):
    total_count: int
    results: list[_OdsDataset]


class HubItem(TypedDict, total=False):
    """An ArcGIS Hub dataset's properties, as its search and item APIs state them."""

    id: str
    type: str | None
    title: str | None
    license: str | None
    licenseInfo: str | None
    created: object
    modified: object
    description: str | None
    snippet: str | None
    source: str | None


class _HubFeature(TypedDict, total=False):
    properties: HubItem | None


class _HubLink(TypedDict, total=False):
    href: str
    rel: str


class _HubPage(TypedDict, total=False):
    features: list[_HubFeature] | None
    numberMatched: int
    links: list[_HubLink] | None


class _DapId(TypedDict, total=False):
    identifier: str


class _DapLink(TypedDict, total=False):
    href: str


class _DapCollection(TypedDict, total=False):
    id: _DapId
    dataCollectionId: int
    landingPage: _DapLink | None
    title: str | None
    description: str | None
    licence: str | None
    published: str | None
    collectionType: str | None
    dataRestricted: str | None
    dataVersionNumber: str | None


class _DapPage(TypedDict, total=False):
    totalResults: str
    next: _DapLink | None
    dataCollections: list[_DapCollection] | None
    errors: object


class _Checked(TypedDict):
    file_count: int
    public_from: NotRequired[str]


class _DapDetail(TypedDict, total=False):
    fileCount: int | None
    accessLevel: str | None
    access: str | None


class AihwResource(TypedDict, total=False):
    """One file or link in the AIHW's list of downloadable resources."""

    resultTitle: str | None
    resultUrl: str  # null on a few live rows, which aihw() skips
    resultDateTimeFormatted: str | None
    resultDescription: str | None
    resultFileType: str | None
    relatedReportTitle: str | None
    relatedReportUrl: str | None
    schemaLicenseUrl: str | None
    schemaDescription: str | None


class _AihwPage(TypedDict):
    results: list[AihwResource]
    totalResults: int


class _Headers(TypedDict, total=False):
    headers: dict[str, str]


class _GetOptions(_Headers, total=False):
    timeout: float
    body: JSONObject


UA = "publicdata.au catalogue (+https://publicdata.au/about/)"
AIHW_LISTING = "https://www.aihw.gov.au/api/search/all-downloadable-resources"
SLUG = "catalogue"
FILENAME = "catalogue.ndjson.gz"


@dataclass(frozen=True)
class Portal:
    code: str
    host: str
    api: str
    jurisdiction: str
    kind: str = "ckan"
    # A portal run by one body names it here, and every record it lists is that body's. A hub
    # that names none files each item under the body its own metadata names.
    publisher: str = ""
    # Organisations on other portals, as portal:org, that copy this one. Their records are
    # dropped whenever this portal has records of its own in the snapshot.
    replaces: tuple[str, ...] = ()
    # A copy whose name matches this, on a portal it replaces, holds this portal's own id in group 1.
    copy_name: str = ""

    def landing(self, name: str) -> str:
        if self.kind == "ods":
            return f"https://{self.host}/explore/dataset/{name}/"
        if self.kind == "hub":
            return f"https://{self.host}/datasets/{name}"
        return f"{self.api.removesuffix('/api/3/action')}/dataset/{name}"


def _council(  # noqa: PLR0913 - the options are keyword-only and named at each call
    code: str, host: str, jur: str, kind: str, publisher: str, *, replaces: Iterable[str] = ()
) -> Portal:
    return Portal(code, host, f"https://{host}", jur, kind, publisher, tuple(replaces))


PORTALS = (
    Portal("gov", "data.gov.au", "https://data.gov.au/data/api/3/action", "cth"),
    Portal("nsw", "data.nsw.gov.au", "https://data.nsw.gov.au/data/api/3/action", "nsw"),
    Portal(
        "vic", "discover.data.vic.gov.au", "https://discover.data.vic.gov.au/api/3/action", "vic"
    ),
    Portal("qld", "www.data.qld.gov.au", "https://www.data.qld.gov.au/api/3/action", "qld"),
    Portal("sa", "data.sa.gov.au", "https://data.sa.gov.au/data/api/3/action", "sa"),
    Portal("wa", "catalogue.data.wa.gov.au", "https://catalogue.data.wa.gov.au/api/3/action", "wa"),
    Portal("nt", "data.nt.gov.au", "https://data.nt.gov.au/api/3/action", "nt"),
    Portal(
        "infra",
        "catalogue.data.infrastructure.gov.au",
        "https://catalogue.data.infrastructure.gov.au/api/3/action",
        "cth",
    ),
    Portal(
        "act", "www.data.act.gov.au", "https://api.us.socrata.com/api/catalog/v1", "act", "socrata"
    ),
    Portal("abs", "data.api.abs.gov.au", "https://data.api.abs.gov.au/rest", "cth", "sdmx"),
    Portal(
        "csiro",
        "data.csiro.au",
        "https://data.csiro.au/dap/ws/v2",
        "cth",
        "dap",
        "CSIRO",
        ("gov:csiro-data-access-portal",),
        r"fedora-pid_csiro-(.+)",
    ),
    Portal(
        "aihw",
        "www.aihw.gov.au",
        AIHW_LISTING,
        "cth",
        "aihw",
        "Australian Institute of Health and Welfare",
        ("gov:aihw",),
    ),
    # Councils with a portal of their own. One that data.gov.au or its state portal already lists
    # in full is left to that copy.
    _council("bne", "data.brisbane.qld.gov.au", "qld", "ods", "Brisbane City Council", replaces=["qld:brisbane-city-council"]),
    _council("melb", "data.melbourne.vic.gov.au", "vic", "ods", "City of Melbourne", replaces=["vic:city-of-melbourne", "gov:city-of-melbourne-open-data"]),
    _council("casey", "data.casey.vic.gov.au", "vic", "ods", "City of Casey", replaces=["vic:city-of-casey", "gov:city-of-casey"]),
    _council("ballarat", "data.ballarat.vic.gov.au", "vic", "ods", "City of Ballarat", replaces=["vic:city-of-ballarat", "gov:city-of-ballarat"]),
    _council("geelong", "www.geelongdataexchange.com.au", "vic", "ods", "City of Greater Geelong", replaces=["vic:city-of-greater-geelong", "gov:city-of-greater-geelong"]),
    _council("corangamite", "data.corangamite.vic.gov.au", "vic", "ods", "Corangamite Shire Council", replaces=["gov:corangamite-shire-council"]),
    _council("hawkesbury", "data.hawkesbury.nsw.gov.au", "nsw", "ods", "Hawkesbury City Council"),
    _council("maitland", "data.maitland.nsw.gov.au", "nsw", "ods", "Maitland City Council"),
    _council("lakemac", "data.lakemac.com.au", "nsw", "ods", "Lake Macquarie City Council", replaces=["gov:lake-macquarie-city-council", "nsw:lakemac"]),
    _council("camden", "data.camden.nsw.gov.au", "nsw", "ods", "Camden Council"),
    _council("liverpool", "data.liverpool.nsw.gov.au", "nsw", "ods", "Liverpool City Council"),
    _council("bmcc", "data.bmcc.nsw.gov.au", "nsw", "ods", "Blue Mountains City Council"),
    _council("cumberland", "data.cumberland.nsw.gov.au", "nsw", "ods", "Cumberland City Council"),
    _council("wpc", "data.wpcouncils.nsw.gov.au", "nsw", "ods", "Western Parkland Councils"),
    _council("campbelltown", "data.campbelltown.nsw.gov.au", "nsw", "ods", "Campbelltown City Council"),
    _council("fairfield", "data.fairfieldcity.nsw.gov.au", "nsw", "ods", "Fairfield City Council"),
    _council("bayside", "nsw-bayside.opendatasoft.com", "nsw", "ods", "Bayside Council"),
    _council("wollondilly", "data.wollondilly.nsw.gov.au", "nsw", "ods", "Wollondilly Shire Council"),
    _council("darwin", "darwin.opendatasoft.com", "nt", "ods", "City of Darwin", replaces=["nt:darwin-city-council"]),
    _council("sydney", "data.cityofsydney.nsw.gov.au", "nsw", "hub", "City of Sydney"),
    _council("goldcoast", "data-goldcoast.opendata.arcgis.com", "qld", "hub", "City of Gold Coast", replaces=["gov:city-of-gold-coast"]),
    _council("sunshine", "data.sunshinecoast.qld.gov.au", "qld", "hub", "Sunshine Coast Council"),
    _council("townsville", "data-tsvcitycouncil.opendata.arcgis.com", "qld", "hub", "Townsville City Council", replaces=["gov:townsville-city-council"]),
    _council("tweed", "data-tweed.opendata.arcgis.com", "nsw", "hub", "Tweed Shire Council"),
    _council("wodonga", "cow-open-data-hub-cityofwodonga.hub.arcgis.com", "vic", "hub", "City of Wodonga"),
    _council("albany", "city-maps-and-data-albanywa.hub.arcgis.com", "wa", "hub", "City of Albany"),
    _council("parramatta", "open-data-parracity.hub.arcgis.com", "nsw", "hub", "City of Parramatta", replaces=["gov:city-of-parramatta"]),
    _council("perth", "geohub-perth.opendata.arcgis.com", "wa", "hub", "City of Perth"),
    _council("latrobe", "geo-latrobecc.hub.arcgis.com", "vic", "hub", "Latrobe City Council"),
)  # fmt: skip
BY_CODE = {p.code: p for p in PORTALS}
# Hosts data.gov.au harvests from that are read directly here, so its copies are duplicates.
DIRECT_HOSTS = {p.host.removeprefix("www.") for p in PORTALS}
PORTAL_BY_HOST = {p.host.removeprefix("www."): p.code for p in PORTALS}

# A resource in one of these can be read and serialised without a browser.
DOWNLOADABLE = {
    "CSV", "TSV", "XLSX", "XLS", "ODS", "JSON", "GEOJSON", "NDJSON", "XML", "PARQUET", "ZIP",
    "SHP", "GPKG", "KML", "KMZ", "GDB", "FGDB", "TAB", "MIF", "SQLITE", "TXT", "API", "SDMX",
}  # fmt: skip
FORMAT_ALIASES = {
    "EXCEL": "XLSX", "MS EXCEL": "XLSX", "EXCEL (.XLSX)": "XLSX", "XLSM": "XLSX",
    "ESRI SHAPEFILE": "SHP", "SHAPEFILE": "SHP", "SHZ": "SHP", "GEO JSON": "GEOJSON",
    "ESRI REST": "API", "REST": "API", "ARCGIS REST": "API", "WFS": "API", "ESRI FILE GEODATABASE": "FGDB",
}  # fmt: skip

# Requested by their Solr names: the harvest origin must be asked for as
# extras_original_harvest_source and comes back keyed original_harvest_source; the type is
# dataset_type. CKAN drops unknown names without an error, so the harvest test pins these.
CKAN_FIELDS = (
    "id,name,title,organization,license_id,metadata_created,metadata_modified,"
    "res_format,dataset_type,notes,extras_original_harvest_source,url"
)
SUMMARY_CHARS = 280


RETRYABLE = (requests.ConnectionError, requests.Timeout)
GET_ATTEMPTS = 5


def _get(
    s: requests.Session,
    url: str,
    params: Params | None = None,
    *,
    timeout: float = 180,
    body: JSONObject | None = None,
    **kw: Unpack[_Headers],
) -> requests.Response:
    if params is not None and body is not None:
        msg = f"{url}: a request takes query parameters or a JSON body, not both"
        raise TypeError(msg)
    for attempt in range(GET_ATTEMPTS):
        try:
            r = (
                s.post(url, json=body, timeout=timeout, **kw)
                if body is not None
                else s.get(url, params=params, timeout=timeout, **kw)
            )
            if (
                r.status_code < HTTPStatus.INTERNAL_SERVER_ERROR
                and r.status_code != HTTPStatus.TOO_MANY_REQUESTS
            ):
                r.raise_for_status()
                return r
        except RETRYABLE:
            if attempt == GET_ATTEMPTS - 1:
                raise
        time.sleep(5 * (attempt + 1))
    r.raise_for_status()
    return r


class PortalError(RuntimeError):
    pass


def get_json(
    s: requests.Session, url: str, params: Params | None = None, **kw: Unpack[_GetOptions]
) -> JSON:
    """A portal's answer parsed as JSON; each caller names the shape it reads."""
    r = _get(s, url, params, **kw)
    try:
        got: JSON = r.json()
    except ValueError:
        raise PortalError(_not_json(url, r)) from None
    return got


def get_json_as[T](
    _shape: type[T],
    s: requests.Session,
    url: str,
    params: Params | None = None,
    **kw: Unpack[_GetOptions],
) -> T:
    """get_json for an answer the caller reads as _shape, which types the result and is not checked."""
    r = _get(s, url, params, **kw)
    try:
        got: T = r.json()
    except ValueError:
        raise PortalError(_not_json(url, r)) from None
    return got


def _not_json(url: str, r: requests.Response) -> str:
    return (
        f"{url}: HTTP {r.status_code}, {r.headers.get('Content-Type') or 'no content type'}, "
        f"{len(r.content)} bytes, not JSON: {r.content[:120]!r}"
    )


# CKAN's default licence register, by title. Checked before any pattern so a title such as
# "Other (Not Open)" cannot match on its words.
KNOWN_LICENCES = {
    "other (open)": "other-open",
    "other (not open)": "other-closed",
    "other (public domain)": "PDM",
    "other (attribution)": "other-at",
    "other (non-commercial)": "other-nc",
    "open data commons attribution license": "ODC-BY-1.0",
    "open data commons open database license (odbl)": "ODBL-1.0",
    "open data commons public domain dedication and license (pddl)": "PDDL-1.0",
    "license not specified": "",
    "licence not specified": "",
    "notspecified": "",
}
OPEN_IDS = {"CC0-1.0", "PDM", "PDDL-1.0", "ODC-BY-1.0", "ODBL-1.0", "other-open", "other-at"}


def licence_id(title: str) -> str:  # noqa: C901, PLR0911, PLR0912 - one branch per licence wording a portal uses
    """A portal's licence title onto an SPDX-style id.

    A title nothing here recognises is kept as the portal states it, so it shows up in review
    rather than being guessed.
    """
    t = re.sub(r"\s+", " ", (title or "").strip()).lower()
    if not t or t in KNOWN_LICENCES:
        return KNOWN_LICENCES.get(t, "")
    if re.search(r"\bnot open\b|\bclosed\b", t):
        return (title or "").strip()
    if re.search(r"\bodbl\b|odc-odbl|open database licen[cs]e", t):
        return "ODBL-1.0"
    if re.search(r"\bodc-by\b|open data commons attribution", t):
        return "ODC-BY-1.0"
    if re.search(r"\bpddl\b", t):
        return "PDDL-1.0"
    if "zero" in t or t in ("cc0", "cc-zero", "cc0-1.0"):
        return "CC0-1.0"
    if "public domain" in t or t == "pdm":
        return "PDM"
    if re.search(r"creative commons|\bcc\b|^cc-", t):
        parts = ["CC-BY"]
        if re.search(r"non[- ]?commercial|\bnc\b", t):
            parts.append("NC")
        if re.search(r"no[ -]?deriv|\bnd\b", t):
            parts.append("ND")
        if re.search(r"share[ -]?alike|\bsa\b", t):
            parts.append("SA")
        ver = re.search(r"(\d\.\d)", t)
        v = ver.group(1) if ver else ""
        au = "australia" in t or t.endswith("-au") or " au" in t
        if v:
            parts.append(v)
            if au and v in ("2.5", "3.0"):
                parts.append("AU")
        return "-".join(parts)
    return (title or "").strip()


def cc_url(url: str) -> str:
    """The licence a creativecommons.org deed URL names, such as CC-BY-3.0-AU."""
    m = re.search(r"creativecommons\.org/licenses/([a-z-]+)/(\d\.\d)(/au)?", url.lower())
    if not m:
        return ""
    au = "-AU" if m[3] and m[2] in ("2.5", "3.0") else ""
    return f"CC-{m[1].upper()}-{m[2]}{au}"


def is_open(lic: str) -> bool | None:
    """Whether a licence allows republication with changes.

    True for licences that allow it, False for ones that do not, None when the portal states no
    licence.
    """
    if not lic:
        return None
    if lic in OPEN_IDS:
        return True
    if lic.startswith("CC-BY"):
        return "-NC" not in lic and "-ND" not in lic
    return False


def formats(raw: Iterable[object] | None) -> list[str]:
    out: set[str] = set()
    for f in raw or []:
        for part in re.split(r"[,/;]", str(f)):
            p = part.strip().upper().lstrip(".")
            if p:
                out.add(FORMAT_ALIASES.get(p, p))
    return sorted(out)


def plain_text(notes: str) -> str:
    """A portal's HTML or Markdown description as one line of plain text."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", notes or ""))
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[*_#`>|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def summary(notes: str) -> str:
    t = plain_text(notes)
    if len(t) <= SUMMARY_CHARS:
        return t
    cut = t[:SUMMARY_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(",;:.") + "…"


def _day(iso: str | None) -> str:
    return (iso or "")[:10]


def record_id(portal: str, source_id: str) -> str:
    """A record's id, stable across renames.

    It is CKAN's package UUID, Socrata's four-by-four or the ABS dataflow id, shaped to fit a
    vote key.
    """
    return re.sub(r"[^a-z0-9-]+", "-", f"{portal}-{source_id}".lower()).strip("-")[:64]


def _record(
    portal: Portal,
    *,
    source_id: str,
    licence: str,
    formats: list[str],
    source_host: str = "",
    **kw: Unpack[_Fields],
) -> Record:
    rec = {
        "id": record_id(portal.code, source_id),
        "portal": portal.code,
        "licence": licence,
        "open": is_open(licence),
        "formats": formats,
        "downloadable": bool(set(formats) & DOWNLOADABLE),
        "source_host": source_host,
        **kw,
    }
    # The keys in name order, as the snapshot has always kept them.
    return cast("Record", dict(sorted(rec.items())))


def ckan(portal: Portal, s: requests.Session, log: Log = print) -> tuple[list[Record], int]:
    """A CKAN portal. A copy of a portal read here directly carries copy_of, for harvest to drop."""
    licences = get_json_as(_NamedList, s, f"{portal.api}/license_list")
    lic_titles = {x["id"]: x.get("title") or x["id"] for x in licences["result"]}
    orgs: dict[str, str] = {}
    off = 0
    while True:
        page = get_json_as(
            _NamedList,
            s,
            f"{portal.api}/organization_list",
            {"all_fields": "true", "limit": 25, "offset": off},
        )["result"]
        new = [o for o in page if o["name"] not in orgs]
        if not new:
            break
        orgs.update({o["name"]: (o.get("title") or o["name"]).strip() for o in new})
        off += len(page)
    out: list[Record] = []
    dropped, start, rows = 0, 0, 1000
    while True:
        res = get_json_as(
            _CkanSearch,
            s,
            f"{portal.api}/package_search",
            {
                "rows": rows,
                "start": start,
                "sort": "id asc",
                "fl": CKAN_FIELDS,
                "include_private": "false",
            },
        )["result"]
        batch = res["results"]
        for p in batch:
            harvest = p.get("original_harvest_source") or p.get("extras_original_harvest_source")
            if isinstance(harvest, str):
                try:
                    harvest = json.loads(harvest)
                except ValueError:
                    harvest = {"site_url": harvest}
            origin = urlparse((harvest or {}).get("site_url") or "").hostname or ""
            host = origin.removeprefix("www.")
            lic_raw = p.get("license_id") or ""
            org = p.get("organization") or ""
            if isinstance(org, dict):
                org = org.get("name") or ""
            rec = _record(
                portal,
                source_id=p["id"],
                name=p.get("name") or "",
                title=re.sub(r"\s+", " ", p.get("title") or p.get("name") or "").strip(),
                org=org,
                org_title=orgs.get(org, org),
                kind=p.get("dataset_type") or p.get("type") or "dataset",
                licence=licence_id(lic_titles.get(lic_raw, lic_raw)),
                licence_title=lic_titles.get(lic_raw, lic_raw),
                formats=formats(p.get("res_format")),
                created=_day(p.get("metadata_created")),
                modified=_day(p.get("metadata_modified")),
                url=portal.landing(p.get("name") or p["id"]),
                summary=summary(p.get("notes") or ""),
                harvested_from=origin if origin and origin != portal.host else "",
                source_host=(urlparse(p.get("url") or "").hostname or "").removeprefix("www."),
            )
            # data.gov.au names itself as the origin of records it holds first hand.
            if (
                portal.code == "gov"
                and host in DIRECT_HOSTS
                and host != portal.host.removeprefix("www.")
            ):
                dropped += 1
                rec["copy_of"] = (harvest or {}).get("href") or ""
            out.append(rec)
        start += len(batch)
        log(f"catalogue {portal.code}: {start}/{res['count']}")
        if not batch or start >= res["count"]:
            break
    return out, dropped


def socrata(portal: Portal, s: requests.Session, log: Log = print) -> tuple[list[Record], int]:
    out: list[Record] = []
    off, after = 0, ""
    while True:
        # scroll_id pages in id order, starting from an empty one, and stays stable while the
        # catalogue changes. Offsets over the default relevance order repeat and skip rows.
        params: Params = {"domains": portal.host, "limit": 100, "scroll_id": after}
        res = get_json_as(_SocrataPage, s, portal.api, params)
        for r in res["results"]:
            x, meta = r["resource"], r.get("metadata") or {}
            kind = x.get("type") or ""
            org = (x.get("attribution") or "").strip()
            lic_title = meta.get("license") or ""
            out.append(
                _record(
                    portal,
                    source_id=x["id"],
                    name=x["id"],
                    title=re.sub(r"\s+", " ", x.get("name") or "").strip(),
                    org=_org_slug(org),
                    org_title=org,
                    kind=kind,
                    licence=licence_id(lic_title),
                    licence_title=lic_title,
                    formats=["API", "CSV", "JSON"] if kind == "dataset" else [],
                    created=_day(x.get("createdAt")),
                    modified=_day(x.get("data_updated_at") or x.get("updatedAt")),
                    url=r.get("permalink") or r.get("link") or "",
                    summary=summary(x.get("description") or ""),
                    harvested_from="",
                )
            )
        off += len(res["results"])
        log(f"catalogue {portal.code}: {off}/{res['resultSetSize']}")
        if not res["results"]:
            break
        after = res["results"][-1]["resource"]["id"]
    return out, 0


def sdmx(portal: Portal, s: requests.Session, log: Log = print) -> tuple[list[Record], int]:
    """ABS dataflows. The ABS states CC BY 4.0 for its statistics unless a release says otherwise."""
    flows = get_json_as(
        _SdmxAnswer,
        s,
        f"{portal.api}/dataflow/ABS",
        {"detail": "allstubs"},
        headers={"Accept": "application/vnd.sdmx.structure+json"},
    )["data"]["dataflows"]
    out: list[Record] = []
    for f in flows:
        fid, ver = f["id"], f.get("version") or "1.0"
        out.append(
            _record(
                portal,
                source_id=fid,
                name=fid,
                title=re.sub(r"\s+", " ", f.get("name") or fid).strip(),
                org="abs",
                org_title="Australian Bureau of Statistics",
                kind="dataflow",
                licence="CC-BY-4.0",
                licence_title="Creative Commons Attribution 4.0 International",
                formats=["API", "CSV", "SDMX"],
                created="",
                modified="",
                url=f"https://dataexplorer.abs.gov.au/vis?df[ds]=ABS_ABS_TOPICS&df[id]={quote(fid)}&df[ag]=ABS&df[vs]={quote(ver)}",
                summary=summary(f.get("description") or ""),
                harvested_from="",
            )
        )
    log(f"catalogue {portal.code}: {len(out)} dataflows")
    return out, 0


def _org_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _council_record(
    portal: Portal,
    *,
    created: str = "",
    body: tuple[str, str] | None = None,
    **kw: Unpack[_CouncilFields],
) -> Record:
    key, title = body or (portal.code, portal.publisher)
    return _record(
        portal,
        org=key,
        org_title=title,
        kind="dataset",
        created=created,
        harvested_from="",
        **kw,
    )


# Opendatasoft's catalogue API pages no further than this many datasets.
ODS_PAGING_LIMIT = 10_000


def ods(portal: Portal, s: requests.Session, log: Log = print) -> tuple[list[Record], int]:
    """An Opendatasoft portal.

    A dataset with records can be exported in every format the platform offers; one without is a
    page of links, which is listed with no files.
    """
    out: list[Record] = []
    off, total = 0, 0
    while True:
        res = get_json_as(
            _OdsPage,
            s,
            f"{portal.api}/api/explore/v2.1/catalog/datasets",
            {"limit": 100, "offset": off},
        )
        total = res["total_count"]
        if total > ODS_PAGING_LIMIT:
            msg = f"{portal.host}: {total} datasets is past the paging limit"
            raise PortalError(msg)
        for x in res["results"]:
            # A dataset federated from another portal carries that portal's name after an @.
            if "@" in x["dataset_id"]:
                continue
            m = (x.get("metas") or {}).get("default") or {}
            lic = m.get("license") or ""
            lid = cc_url(m.get("license_url") or "") or licence_id(lic)
            fmts: list[str] = []
            if x.get("has_records"):
                fmts = ["API", "CSV", "JSON", "XLSX"]
                if "geo" in (x.get("features") or []):
                    fmts += ["GEOJSON", "SHP", "KML"]
            out.append(
                _council_record(
                    portal,
                    source_id=x.get("dataset_uid") or x["dataset_id"],
                    name=x["dataset_id"],
                    title=re.sub(r"\s+", " ", m.get("title") or x["dataset_id"]).strip(),
                    licence=lid,
                    licence_title=lic,
                    formats=formats(fmts),
                    modified=_day(m.get("modified") or ""),
                    url=portal.landing(x["dataset_id"]),
                    summary=summary(m.get("description") or ""),
                )
            )
        off += len(res["results"])
        log(f"catalogue {portal.code}: {off}/{total}")
        if not res["results"] or off >= total:
            break
    return out, 0


# What each ArcGIS item type can be read as. A feature layer is served by its REST API and Hub
# exports it on request.
HUB_FORMATS = {
    "Feature Service": ["API", "CSV", "GEOJSON", "SHP"],
    "Map Service": ["API"],
    "Image Service": ["API"],
    "Vector Tile Service": ["API"],
    "WFS": ["API"],
    "WMS": ["WMS"],
    "CSV": ["CSV"],
    "CSV Collection": ["CSV"],
    "Microsoft Excel": ["XLSX"],
    "Shapefile": ["SHP"],
    "File Geodatabase": ["FGDB"],
    "GeoJson": ["GEOJSON"],
    "KML": ["KML"],
    "KML Collection": ["KML"],
    "GeoPackage": ["GPKG"],
}


def _epoch_day(ms: object) -> str:
    s = str(ms if ms is not None else "").strip()
    if not s.isdigit():
        return ""
    try:
        return (dt.date(1970, 1, 1) + dt.timedelta(milliseconds=int(s))).isoformat()
    except OverflowError:
        return ""


def hub_licence(x: HubItem) -> tuple[str, str]:
    """The licence an ArcGIS Hub dataset states.

    Hub states a licence id, or "custom" or "none" with the terms, often a link to a Creative
    Commons deed, written out in licenseInfo.
    """
    lic = (x.get("license") or "").strip()
    raw = x.get("licenseInfo") or ""
    text = summary(raw)
    if lic.lower() not in ("", "none", "custom"):
        return licence_id(lic), text or lic
    found = cc_url(raw)
    if found or lic.lower() in ("", "none"):
        return found, text
    named = licence_id(text)
    return (named if named != text else "custom"), text


def hub_org(portal: Portal, x: HubItem) -> tuple[str, str] | None:
    """The organisation a Hub item is filed under, as (key, title), or None for the portal's own.

    A hub of many bodies names each item's body in its Source field. The owner is an account
    name, sometimes a person's, so an item with no Source is filed as unknown.
    """
    if portal.publisher:
        return None
    title = re.sub(r"\s+", " ", x.get("source") or "").strip()
    return _org_slug(title), title


def hub(portal: Portal, s: requests.Session, log: Log = print) -> tuple[list[Record], int]:
    """An ArcGIS Hub site, read through its OGC Records search of the site's own catalogue."""
    out: list[Record] = []
    n = 0
    url = f"{portal.api}/api/search/v1/collections/dataset/items"
    params: Params | None = {"limit": 100}
    while True:
        res = get_json_as(_HubPage, s, url, params)
        feats = res.get("features") or []
        for f in feats:
            x = f.get("properties") or {}
            lic, lic_title = hub_licence(x)
            kind = x.get("type") or ""
            out.append(
                _council_record(
                    portal,
                    source_id=x["id"],
                    name=x["id"],
                    title=re.sub(r"\s+", " ", x.get("title") or x["id"]).strip(),
                    licence=lic,
                    licence_title=lic_title,
                    formats=formats(HUB_FORMATS.get(kind, [kind] if kind else [])),
                    created=_epoch_day(x.get("created")),
                    modified=_epoch_day(x.get("modified")),
                    url=portal.landing(x["id"]),
                    summary=summary(x.get("description") or x.get("snippet") or ""),
                    body=hub_org(portal, x),
                )
            )
        n += len(feats)
        log(f"catalogue {portal.code}: {n}/{res.get('numberMatched')}")
        nxt = next((x.get("href") for x in res.get("links") or [] if x.get("rel") == "next"), "")
        if not feats or not nxt:
            break
        url, params = nxt, None
    return out, 0


# The Data Access Portal answers a search with at most this many collections, and pages of 100.
DAP_LIMIT = 10_000
DAP_PAGE = 100
DAP_FIRST = dt.date(1990, 1, 1)
DAP_TIMEOUT = 60
# Publication dates are compared in one fixed offset so day windows meet without a gap.
DAP_TZ = "+10:00"
# Full records read per harvest; the rest wait for a later week and are not downloadable yet.
DAP_DETAILS = 2_000


def _dap_record(portal: Portal, x: _DapCollection, detail: _Checked | None) -> Record:
    pid = x["id"]["identifier"]
    kind = (x.get("collectionType") or "").strip().lower()
    lic = (x.get("licence") or "").strip()
    rec = _record(
        portal,
        source_id=pid.removeprefix("csiro:"),
        name=pid,
        title=re.sub(r"\s+", " ", x.get("title") or pid).strip(),
        org=portal.code,
        org_title=portal.publisher,
        kind="dataset" if kind == "data" else kind,
        licence=licence_id(lic),
        licence_title=lic,
        # Unrestricted files can be fetched through the web service without a request.
        formats=["API"] if detail and detail["file_count"] else [],
        created="",
        modified=_day(x.get("published")),
        url=(x.get("landingPage") or {}).get("href") or "",
        summary=summary(x.get("description") or ""),
        harvested_from="",
    )
    rec["source_version"] = f"{x.get('dataVersionNumber') or ''}@{x.get('published') or ''}"
    if detail:
        rec["file_count"] = detail["file_count"]
        if detail.get("public_from"):
            rec["public_from"] = detail["public_from"]
    return rec


MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


def _embargo_end(access: str) -> str:
    """The day a DAP access statement such as "restricted until 30 Sept 2027" ends, or ''."""
    m = re.search(r"until (\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})", access)
    month = MONTHS.get(m[2].lower(), 0) if m else 0
    if not m or not month:
        return ""
    try:
        return dt.date(int(m[3]), month, int(m[1])).isoformat()
    except ValueError:
        return ""


def _dap_checked(got: _DapDetail) -> _Checked | None:
    n = got.get("fileCount")
    if not isinstance(n, int):
        return None
    if got.get("accessLevel") == "Public":
        return {"file_count": n}
    return {"file_count": 0, "public_from": _embargo_end(got.get("access") or "")}


def _dap_page(
    portal: Portal, s: requests.Session, first: dt.date, last: dt.date, page: int
) -> _DapPage:
    params: Params = {
        "rpp": DAP_PAGE,
        "p": page,
        "sb": "RECENT",
        "psd": f"{first}T00:00:00{DAP_TZ}",
        "ped": f"{last}T23:59:59.999{DAP_TZ}",
    }
    res = get_json_as(
        _DapPage,
        s,
        f"{portal.api}/collections",
        params,
        timeout=DAP_TIMEOUT,
        headers={"Accept": "application/json"},
    )
    if res.get("errors") or "totalResults" not in res or "dataCollections" not in res:
        msg = f"{portal.host}: page {page} of {first} to {last} is not a search answer: {str(res)[:200]}"
        raise PortalError(msg)
    return res


def _dap_listing(portal: Portal, s: requests.Session, log: Log) -> list[_DapCollection]:
    """Every collection, by publication date in windows halved until the search pages each in full."""
    out: list[_DapCollection] = []
    windows = [(DAP_FIRST, dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1))]
    while windows:
        first, last = windows.pop()
        page = 1
        while True:
            res = _dap_page(portal, s, first, last, page)
            total = str(res.get("totalResults") or "0")
            if page == 1 and (not total.isdigit() or int(total) >= DAP_LIMIT):
                if first == last:
                    msg = f"{portal.host}: {total} collections published on {first}"
                    raise PortalError(msg)
                mid = first + (last - first) // 2
                windows += [(first, mid), (mid + dt.timedelta(days=1), last)]
                break
            batch = res.get("dataCollections") or []
            if not batch and int(total) > (page - 1) * DAP_PAGE:
                msg = f"{portal.host}: page {page} of {first} to {last} is empty of {total}"
                raise PortalError(msg)
            out += batch
            log(f"catalogue {portal.code}: {first} to {last}, page {page}, {len(out)} so far")
            if not batch or not res.get("next") or page * DAP_PAGE >= DAP_LIMIT:
                break
            page += 1
    return out


def _dap_details(
    portal: Portal, s: requests.Session, log: Log, todo: list[tuple[str, _DapCollection]]
) -> tuple[dict[str, _Checked], int]:
    """The answers of the full records read, by record id, and how many could not be read."""
    done: dict[str, _Checked] = {}
    failed = 0
    for rid, x in todo:
        try:
            got = get_json_as(
                _DapDetail,
                s,
                f"{portal.api}/collections/{x['dataCollectionId']}",
                timeout=DAP_TIMEOUT,
                headers={"Accept": "application/json"},
            )
        except (requests.RequestException, ValueError, PortalError) as e:
            # One collection's record failing every week must not stop the whole portal.
            failed += 1
            log(f"catalogue {portal.code}: the full record of {rid} could not be read: {e}")
            continue
        if c := _dap_checked(got):
            done[rid] = c
    return done, failed


def dap(
    portal: Portal, s: requests.Session, log: Log = print, previous: Iterable[Record] = ()
) -> tuple[list[Record], int]:
    """CSIRO's Data Access Portal.

    A collection is downloadable when its full record offers the public at least one file. The
    full record is read only for an open, unrestricted data collection whose version the previous
    snapshot did not read, or whose stated embargo has ended, and at most DAP_DETAILS of them in
    one harvest. A collection never read is not downloadable. One due a reread that is past the
    cap, or whose full record cannot be read, keeps its earlier answer and the version that answer
    was read at, so the next harvest reads it again.
    """
    found = _dap_listing(portal, s, log)
    if not found:
        msg = f"{portal.host}: the search listed no collections"
        raise PortalError(msg)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    before = {r["id"]: r for r in previous if "file_count" in r}
    listed = [_dap_record(portal, x, None) for x in found]
    done: dict[str, _Checked] = {}
    todo: list[tuple[str, _DapCollection]] = []
    for x, r in zip(found, listed, strict=True):
        if x.get("dataRestricted") != "FALSE" or r["kind"] != "dataset" or not r["open"]:
            continue
        b = before.get(r["id"])
        ends = b.get("public_from", "") if b else ""
        if b and b.get("source_version") == r["source_version"] and not (ends and ends <= today):
            done[r["id"]] = {"file_count": b["file_count"]}
            if ends:
                done[r["id"]]["public_from"] = ends
        else:
            todo.append((r["id"], x))
    todo.sort(key=lambda t: t[0])
    read, failed = _dap_details(portal, s, log, todo[:DAP_DETAILS])
    done |= read
    # A collection not read this time keeps the answer it had, under the version it was read at.
    stale = {rid: before[rid] for rid, _ in todo if rid not in done and rid in before}
    for rid, b in stale.items():
        done[rid] = {"file_count": b["file_count"]}
        if b.get("public_from"):
            done[rid]["public_from"] = b["public_from"]
    log(
        f"catalogue {portal.code}: {min(len(todo), DAP_DETAILS) - failed} of {len(todo)} full "
        f"records read, {failed} failed"
    )
    out = [
        _dap_record(portal, x, done[r["id"]]) if r["id"] in done else r
        for x, r in zip(found, listed, strict=True)
    ]
    for r in out:
        if r["id"] in stale:
            r["source_version"] = stale[r["id"]]["source_version"]
    return out, 0


# The listing answers no more than this many resources a page, whatever it is asked for.
AIHW_PAGE_SIZE = 50
# What record_id keeps of a source id after "aihw-".
AIHW_KEY_CHARS = 59
AIHW_HOSTS = {"www.aihw.gov.au", "aihw.gov.au"}
AIHW_LICENCE = ("CC-BY-4.0", "Creative Commons Attribution 4.0 International")
# Words by which a resource sets its own terms apart from the copyright page's licence.
AIHW_OTHER_TERMS = re.compile(
    r"third[- ]party|copyright|©|all rights reserved|not (?:be )?licen[cs]ed|permission"
    r"|terms of use|restricted",
    re.IGNORECASE,
)
# Path segments that name a part of a report rather than the report.
AIHW_GENERIC = {
    "about", "appendix", "contents", "data", "data-downloads", "data-tables", "download",
    "download-data", "download-data-tables", "downloads", "index", "notes", "overview", "report",
    "reports", "resources", "summary", "technical-notes", "web-report",
}  # fmt: skip


def aihw_terms(x: AihwResource) -> tuple[str, str]:
    """The licence one AIHW resource is under.

    The copyright page (https://www.aihw.gov.au/copyright) releases the Institute's material under
    CC BY 4.0, except third-party material, so only a file on the Institute's own site takes it. A
    resource that names another licence or mentions other terms is kept as it states them, so it
    is never marked open, and one hosted elsewhere states no licence.
    """
    url = (x.get("schemaLicenseUrl") or "").strip()
    if url and cc_url(url) != AIHW_LICENCE[0]:
        return cc_url(url) or url, url
    said = (x.get("resultTitle"), x.get("resultDescription"), x.get("schemaDescription"))
    words = " ".join(w for w in said if w)
    if AIHW_OTHER_TERMS.search(words):
        text = summary(words)
        return text, text
    if (urlparse(_aihw_url(x["resultUrl"])).hostname or "") not in AIHW_HOSTS:
        return "", ""
    return AIHW_LICENCE


def _aihw_url(path: str) -> str:
    return "https://www.aihw.gov.au" + path if path.startswith("/") else path


def _aihw_rank(term: tuple[str, str]) -> tuple[bool, bool, bool]:
    """Orders a report's terms so the least open comes first and the default last.

    A stated licence that is not open comes before no licence at all.
    """
    return is_open(term[0]) is True, not term[0], term == AIHW_LICENCE


def _aihw_id(url: str) -> str:
    """A file's media id, or a report's last path segment with its parents while it is generic.

    Two reports under one slug in different topics are the same report listed twice and share
    the id. A file on another site has that site's host in front. A key too long for a vote key
    keeps its start and a hash of the key as record_id folds it.
    """
    u = urlparse(url)
    parts = [p for p in u.path.split("/") if p] or [url]
    if parts[0] == "getmedia" and len(parts) > 1:
        key = parts[1]
    else:
        i = len(parts) - 1
        while i > 0 and parts[i].lower() in AIHW_GENERIC:
            i -= 1
        key = "-".join(parts[i:])
    host = (u.hostname or "").lower()
    if host and host not in AIHW_HOSTS:
        key = f"{host}-{key}"
    if len(key) > AIHW_KEY_CHARS:
        folded = re.sub(r"[^a-z0-9-]+", "-", key.lower()).strip("-")
        key = f"{key[: AIHW_KEY_CHARS - 9]}-{hashlib.sha256(folded.encode()).hexdigest()[:8]}"
    return key


def aihw(portal: Portal, s: requests.Session, log: Log = print) -> tuple[list[Record], int]:
    """The AIHW's data downloads, one record per report with the files it lists.

    A file that names no report is a record of its own, at the file's address. A report with no
    file in a format we can read, such as a PDF publication, is left out.
    """
    groups: dict[str, dict[str, AihwResource]] = {}
    keys: dict[str, str] = {}
    n, page = 0, 1
    while True:
        res = get_json_as(
            _AihwPage,
            s,
            portal.api,
            body={"itemsPerPage": AIHW_PAGE_SIZE, "page": page, "showReportLink": True},
            headers={"Accept": "application/json"},
        )
        batch = res["results"]
        if not batch and n < res["totalResults"]:
            msg = f"{portal.host}: page {page} is empty at {n} of {res['totalResults']} resources"
            raise PortalError(msg)
        for x in batch:
            # A resource with no file address is a link to nothing we can download.
            if not x.get("resultUrl"):
                continue
            url = _aihw_url(x.get("relatedReportUrl") or x["resultUrl"])
            # Paging by number can repeat a file when the list changes between pages.
            key = _aihw_id(url)
            # Keys that differ only in what record_id folds are one record.
            rid = record_id(portal.code, key)
            groups.setdefault(rid, {}).setdefault(x["resultUrl"], x)
            keys[rid] = min(keys.get(rid, key), key)
        n += len(batch)
        log(f"catalogue {portal.code}: {n}/{res['totalResults']}")
        if n >= res["totalResults"]:
            break
        page += 1
    out: list[Record] = []
    for rid, by_file in sorted(groups.items()):
        key = keys[rid]
        files = [by_file[u] for u in sorted(by_file)]
        fmts = formats(x.get("resultFileType") for x in files if x.get("resultFileType"))
        if not set(fmts) & DOWNLOADABLE:
            continue
        lic, lic_title = min((aihw_terms(x) for x in files), key=_aihw_rank)
        days = sorted(d for x in files if (d := _day(x.get("resultDateTimeFormatted"))))
        url = min(_aihw_url(x.get("relatedReportUrl") or x["resultUrl"]) for x in files)
        first = next(
            x for x in files if _aihw_url(x.get("relatedReportUrl") or x["resultUrl"]) == url
        )
        title = first.get("relatedReportTitle") or first.get("resultTitle") or url
        about = next((x.get("resultDescription") for x in files if x.get("resultDescription")), "")
        rec = _council_record(
            portal,
            source_id=key,
            name=key,
            title=re.sub(r"\s+", " ", title).strip(),
            licence=lic,
            licence_title=lic_title,
            formats=fmts,
            created=days[0] if days else "",
            modified=days[-1] if days else "",
            url=url,
            summary=summary(about or "; ".join(x.get("resultTitle") or "" for x in files)),
        )
        # The files' addresses, so a register entry fetching one marks this record served.
        rec["files"] = [_aihw_url(x["resultUrl"]) for x in files]
        out.append(cast("Record", dict(sorted(rec.items()))))
    return out, 0


HARVESTERS = {
    "ckan": ckan,
    "socrata": socrata,
    "sdmx": sdmx,
    "ods": ods,
    "hub": hub,
    "dap": dap,
    "aihw": aihw,
}


def harvest(
    portals: Iterable[Portal] = PORTALS,
    log: Log = print,
    previous: list[Record] | None = None,
    previous_version: str = "",
) -> tuple[list[Record], dict[str, PortalStats]]:
    """A portal that cannot be read keeps its records from the previous snapshot.

    The stats say so, so an outage never reads as datasets withdrawn. With no previous snapshot
    its records are absent and the stats say it was not read. A previous version whose snapshot
    is not in the store stops the harvest before any portal is read.
    """
    if previous is None and previous_version:
        # Without it a portal's records cannot be kept and every carried copy would be dropped.
        msg = (
            f"the {previous_version} snapshot is not in the store to carry its records and "
            "copies forward; run `publicdata store pull --only catalogue`"
        )
        raise PortalError(msg)
    s = requests.Session()
    s.headers["User-Agent"] = UA
    records: list[Record] = []
    copies: list[Record] = []
    stats: dict[str, PortalStats] = {}
    for p in portals:
        try:
            # Only the DAP reader reuses earlier answers, so only it is given the last records.
            if p.kind == "dap":
                recs, dropped = dap(p, s, log, [r for r in previous or [] if r["portal"] == p.code])
            else:
                recs, dropped = HARVESTERS[p.kind](p, s, log)
        except (requests.RequestException, PortalError, KeyError, ValueError, TypeError) as e:
            kept = [r for r in previous or [] if r["portal"] == p.code]
            records += kept
            stats[p.code] = {
                "records": len(kept),
                "error": str(e)[:300],
                "carried_from": previous_version if kept else None,
            }
            log(
                f"catalogue: {p.host} could not be read; {len(kept)} records kept from {previous_version or 'no earlier snapshot'}: {e}"
            )
            continue
        copies += [r for r in recs if "copy_of" in r]
        recs = [r for r in recs if "copy_of" not in r]
        records += recs
        stats[p.code] = {"records": len(recs), "dropped_duplicates": dropped}
    moved: dict[str, str] = {}
    kept = _drop_copies(records, portals, stats, moved)
    pages = {r["id"]: r["url"] for r in records + copies if r.get("url")}
    records = kept
    seen: set[str] = set()
    unique: list[Record] = []
    for r in sorted(records, key=lambda r: r["id"]):
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)
    return _carry(unique, copies, moved, previous or [], pages), stats


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def named_copy(p: Portal, name: str) -> str:
    """The id of the record on p that a copy named `name` reproduces, when its name says, or ''."""
    m = re.fullmatch(p.copy_name, name) if p.copy_name else None
    return record_id(p.code, m[1]) if m else ""


def _drop_copies(
    records: list[Record],
    portals: Iterable[Portal],
    stats: dict[str, PortalStats],
    moved: dict[str, str] | None = None,
) -> list[Record]:
    """Drop the copies of records that a portal read directly now lists itself.

    A record in an organisation a portal replaces is a copy. When its name carries the id of a
    record on that portal, it is dropped if that record is listed and kept if not. Otherwise it
    is dropped when the portal lists the same title or the record's source is on the portal.
    Anything else that organisation holds is kept, since it may come from somewhere the portal
    does not. A dropped copy goes into `moved`, mapped to the record its name carries or else to
    the one record on the portal with its title.
    """
    ids = {r["id"] for r in records}
    have = {r["portal"] for r in records}
    scope = {o: p for p in portals if p.code in have for o in p.replaces}
    if not scope:
        return records
    titles: dict[str, dict[str, list[str]]] = {}
    for r in records:
        if r["portal"] in {p.code for p in scope.values()}:
            titles.setdefault(r["portal"], {}).setdefault(_title_key(r["title"]), []).append(
                r["id"]
            )
    kept: list[Record] = []
    for r in records:
        p = scope.get(f"{r['portal']}:{r['org'] or 'unknown'}")
        named = named_copy(p, r["name"]) if p else ""
        same = [] if named or not p else titles.get(p.code, {}).get(_title_key(r["title"]), [])
        if named and named not in ids:
            kept.append(r)
            continue
        if p and (named or same or r.get("source_host") == p.host.removeprefix("www.")):
            st = stats.setdefault(r["portal"], {})
            st["dropped_duplicates"] = st.get("dropped_duplicates", 0) + 1
            if moved is not None and (named or len(same) == 1):
                moved[r["id"]] = named or same[0]
            continue
        kept.append(r)
    return kept


def bare_url(url: str) -> str:
    """A URL as host and path, which is how two portals' links to one page compare."""
    u = urlparse(url.strip())
    return (u.hostname or "").removeprefix("www.") + u.path.rstrip("/")


def _original(dup: Record, by_url: Mapping[str, str], live: Container[str]) -> str:
    """The id of the record a data.gov.au copy was harvested from, or "".

    The copy names the original's page, and CKAN's harvester keeps the original's package id.
    The portal the id is looked up on is the page's, or else the harvest source's.
    """
    href = dup.get("copy_of", "")
    if href and bare_url(href) in by_url:
        return by_url[bare_url(href)]
    hosts = ((urlparse(href).hostname or ""), dup.get("harvested_from", ""))
    code = next((c for h in hosts if (c := PORTAL_BY_HOST.get(h.removeprefix("www.")))), "")
    if not code:
        return ""
    same = record_id(code, dup["id"].split("-", 1)[1])
    return same if same in live else ""


def _carry(
    records: list[Record],
    copies: list[Record],
    moved: dict[str, str],
    previous: list[Record],
    pages: Mapping[str, str] | None = None,
) -> list[Record]:
    """Give each record the ids of the copies it replaced, so their votes and issues follow it.

    A copy with no original found is dropped and left out. A record keeps the copies it replaced
    in earlier snapshots, since a portal that cannot be read this time has no copies to match.
    `pages` holds the page of each record harvested, by id, so a copy's page is kept beside it.
    """
    live = {r["id"] for r in records}
    by_url = {bare_url(r["url"]): r["id"] for r in records if r.get("url")}
    to: dict[str, str] = {c: r["id"] for r in previous for c in r.get("copies", [])}
    page = {c: u for r in previous for c, u in r.get("copy_pages", {}).items()}
    page |= {c: u for c, u in (pages or {}).items() if c not in live}
    to |= moved
    # An original that was itself dropped as a copy is followed on below.
    known = live | to.keys()
    for dup in copies:
        if found := _original(dup, by_url, known):
            to[dup["id"]] = found
    by_target: dict[str, list[str]] = {}
    for c, first in to.items():
        t, seen = first, {c}
        # A record that is itself replaced passes its copies on.
        while t not in live and t in to and t not in seen:
            seen.add(t)
            t = to[t]
        if c not in live and t in live:
            by_target.setdefault(t, []).append(c)
    for r in records:
        r.pop("copies", None)
        r.pop("copy_pages", None)
        if r["id"] in by_target:
            r["copies"] = sorted(by_target[r["id"]])
            if found_pages := {c: page[c] for c in r["copies"] if c in page}:
                r["copy_pages"] = found_pages
    return records


def encode(records: Iterable[Record]) -> bytes:
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    return gzip.compress(body.encode("utf-8"), compresslevel=9, mtime=0)


def decode(data: bytes) -> list[Record]:
    return [json.loads(line) for line in gzip.decompress(data).decode("utf-8").splitlines() if line]


def latest(store_dir: Path) -> store.Manifest | None:
    ms = store.manifests(store_dir, SLUG)
    return ms[-1] if ms else None


def load(store_dir: Path) -> list[Record]:
    m = latest(store_dir)
    if not m:
        return []
    store.verify(store_dir, m)
    return decode(store.source_path(store_dir, m).read_bytes())


def fetch(
    store_dir: Path,
    log: Log = print,
    portals: Iterable[Portal] = PORTALS,
    today: str | None = None,
) -> store.Manifest | None:
    prev = latest(store_dir)
    previous = load(store_dir) if prev and store.source_path(store_dir, prev).exists() else None
    records, stats = harvest(portals, log, previous, prev.version if prev else "")
    data = encode(records)
    digest = hashlib.sha256(data).hexdigest()
    if prev and prev.sha256 == digest:
        log("catalogue: unchanged")
        return None
    now = dt.datetime.now(dt.UTC)
    version = today or now.date().isoformat()
    if version in {x.version for x in store.manifests(store_dir, SLUG)}:
        # A version is never rewritten. A second harvest on the same day waits for a later date.
        log(f"catalogue: a different snapshot for {version} is already stored; not overwriting")
        return None
    m = store.Manifest(
        dataset=SLUG,
        version=version,
        as_at=version,
        fetched_at=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        sha256=digest,
        bytes=len(data),
        filename=FILENAME,
        encoding="utf-8",
        source={"portals": {p.code: p.host for p in portals}, "stats": stats},
        licence={"id": "various", "note": "Each record carries the licence its portal states."},
    )
    store.write(store_dir, m, data)
    log(f"catalogue: new version {version} ({len(records)} records)")
    return m
