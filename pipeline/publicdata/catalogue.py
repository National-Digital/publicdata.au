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
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from .jsontypes import JSON
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


# The parts of each portal's API answers that the harvesters read.
class _Named(TypedDict, total=False):
    id: str
    name: str
    title: str | None


class _NamedList(TypedDict):
    result: list[_Named]


class _Harvest(TypedDict, total=False):
    site_url: str | None


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


class _HubFeature(TypedDict, total=False):
    properties: HubItem | None


class _HubLink(TypedDict, total=False):
    href: str
    rel: str


class _HubPage(TypedDict, total=False):
    features: list[_HubFeature] | None
    numberMatched: int
    links: list[_HubLink] | None


class _Headers(TypedDict, total=False):
    headers: dict[str, str]


class _GetOptions(_Headers, total=False):
    timeout: float


UA = "publicdata.au catalogue (+https://publicdata.au/about/)"
SLUG = "catalogue"
FILENAME = "catalogue.ndjson.gz"


@dataclass(frozen=True)
class Portal:
    code: str
    host: str
    api: str
    jurisdiction: str
    kind: str = "ckan"
    # A portal run by one body names it here, and every record it lists is that body's.
    publisher: str = ""
    # Organisations on other portals, as portal:org, that copy this one. Their records are
    # dropped whenever this portal has records of its own in the snapshot.
    replaces: tuple[str, ...] = ()

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
    **kw: Unpack[_Headers],
) -> requests.Response:
    for attempt in range(GET_ATTEMPTS):
        try:
            r = s.get(url, params=params, timeout=timeout, **kw)
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
        if re.search(r"non-?commercial|\bnc\b", t):
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
            # data.gov.au names itself as the origin of records it holds first hand.
            if (
                portal.code == "gov"
                and host in DIRECT_HOSTS
                and host != portal.host.removeprefix("www.")
            ):
                dropped += 1
                continue
            lic_raw = p.get("license_id") or ""
            org = p.get("organization") or ""
            if isinstance(org, dict):
                org = org.get("name") or ""
            out.append(
                _record(
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
            )
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
                    org=re.sub(r"[^a-z0-9]+", "-", org.lower()).strip("-"),
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


def _council_record(portal: Portal, *, created: str = "", **kw: Unpack[_CouncilFields]) -> Record:
    return _record(
        portal,
        org=portal.code,
        org_title=portal.publisher,
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
                )
            )
        n += len(feats)
        log(f"catalogue {portal.code}: {n}/{res.get('numberMatched')}")
        nxt = next((x.get("href") for x in res.get("links") or [] if x.get("rel") == "next"), "")
        if not feats or not nxt:
            break
        url, params = nxt, None
    return out, 0


HARVESTERS = {"ckan": ckan, "socrata": socrata, "sdmx": sdmx, "ods": ods, "hub": hub}


def harvest(
    portals: Iterable[Portal] = PORTALS,
    log: Log = print,
    previous: list[Record] | None = None,
    previous_version: str = "",
) -> tuple[list[Record], dict[str, PortalStats]]:
    """A portal that cannot be read keeps its records from the previous snapshot.

    The stats say so, so an outage never reads as datasets withdrawn. With no previous snapshot
    its records are absent and the stats say it was not read.
    """
    s = requests.Session()
    s.headers["User-Agent"] = UA
    records: list[Record] = []
    stats: dict[str, PortalStats] = {}
    for p in portals:
        try:
            recs, dropped = HARVESTERS[p.kind](p, s, log)
        except (requests.RequestException, PortalError, KeyError, ValueError, TypeError) as e:
            if previous is None and previous_version:
                msg = (
                    f"{p.host} could not be read and the {previous_version} snapshot is not in the "
                    "store to carry its records forward; run `publicdata store pull --only catalogue`"
                )
                raise PortalError(msg) from e
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
        records += recs
        stats[p.code] = {"records": len(recs), "dropped_duplicates": dropped}
    records = _drop_copies(records, portals, stats)
    seen: set[str] = set()
    unique: list[Record] = []
    for r in sorted(records, key=lambda r: r["id"]):
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)
    return unique, stats


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def _drop_copies(
    records: list[Record], portals: Iterable[Portal], stats: dict[str, PortalStats]
) -> list[Record]:
    """Drop the copies of records that a council portal now lists itself.

    A record in an organisation a council portal replaces is dropped when the council portal
    lists the same title or the record's source is on the council portal. Anything else that
    organisation holds is kept, since it may come from somewhere the council portal does not.
    """
    have = {r["portal"] for r in records}
    scope = {o: p for p in portals if p.code in have for o in p.replaces}
    if not scope:
        return records
    titles: dict[str, set[str]] = {}
    for r in records:
        if r["portal"] in {p.code for p in scope.values()}:
            titles.setdefault(r["portal"], set()).add(_title_key(r["title"]))
    kept: list[Record] = []
    for r in records:
        p = scope.get(f"{r['portal']}:{r['org'] or 'unknown'}")
        if p and (
            _title_key(r["title"]) in titles.get(p.code, ())
            or r.get("source_host") == p.host.removeprefix("www.")
        ):
            st = stats.setdefault(r["portal"], {})
            st["dropped_duplicates"] = st.get("dropped_duplicates", 0) + 1
            continue
        kept.append(r)
    return kept


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
